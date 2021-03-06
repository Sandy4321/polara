from timeit import default_timer as timer
import pandas as pd
import numpy as np
import scipy as sp
import scipy.sparse
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import svds
from polara.recommender import data, defaults
from polara.recommender.evaluation import get_hits, get_relevance_scores, get_ranking_scores
from polara.recommender.utils import array_split
from polara.lib.hosvd import tucker_als


def get_default(name):
    return defaults.get_config([name])[name]


class RecommenderModel(object):
    _config = ('topk', 'filter_seen', 'switch_positive', 'verify_integrity')
    _pad_const = -1 # used for sparse data

    def __init__(self, recommender_data, switch_positive=None):

        self.data = recommender_data
        self._recommendations = None
        self.method = 'ABC'

        self._topk = get_default('topk')
        self.filter_seen  = get_default('filter_seen')
        # `switch_positive` can be used by other models during construction process
        # (e.g. mymedialite wrapper or any other implicit model); hence, it's
        # better to make it a model attribute, not a simple evaluation argument
        # (in contrast to `on_feedback_level` argument of self.evaluate)
        self.switch_positive  = switch_positive or get_default('switch_positive')
        self.verify_integrity =  get_default('verify_integrity')


    @property
    def recommendations(self):
        if (self._recommendations is None):
            try:
                self._recommendations = self.get_recommendations()
            except AttributeError:
                print '{} model is not ready. Rebuilding.'.format(self.method)
                self.build()
                self._recommendations = self.get_recommendations()
        return self._recommendations


    @property
    def topk(self):
        return self._topk

    @topk.setter
    def topk(self, new_value):
        #support rolling back scenarion for @k calculations
        if (self._recommendations is not None) and (new_value > self._recommendations.shape[1]):
            self._recommendations = None #if topk is too high - recalculate recommendations
        self._topk = new_value


    def build(self):
        raise NotImplementedError('This must be implemented in subclasses')


    def _get_slices_idx(self, shape, result_width=None, scores_multiplier=None, dtypes=None):
        result_width = result_width or self.topk
        if scores_multiplier is None:
            try:
                fdbk_dim = self._feedback_factors.shape
                scores_multiplier = fdbk_dim[0] + 2*fdbk_dim[1]
            except AttributeError:
                scores_multiplier = 1

        slices_idx = array_split(shape, result_width, scores_multiplier, dtypes=dtypes)
        return slices_idx


    def _get_test_data(self):
        try:
            tensor_mode = self._feedback_factors is not None
        except AttributeError:
            tensor_mode = False

        test_data = self.data.test_to_coo(tensor_mode=tensor_mode)
        test_shape = self.data.get_test_shape(tensor_mode=tensor_mode)
        return test_data, test_shape


    def _slice_test_data(self, test_data, start, stop):
        user_coo, item_coo, fdbk_coo = test_data

        slicer = (user_coo>=start) & (user_coo<stop)
        # always slice over users only
        user_slice_coo = user_coo[slicer] - start
        item_slice_coo = item_coo[slicer]
        fdbk_slice_coo = fdbk_coo[slicer]

        return (user_slice_coo, item_slice_coo, fdbk_slice_coo)


    def get_test_matrix(self, test_data, shape, user_slice=None):
        if user_slice:
            start, stop = user_slice
            num_users = stop - start
            coo_data = self._slice_test_data(test_data, start, stop)
        else:
            num_users = shape[0]
            coo_data = test_data

        user_coo, item_coo, fdbk_coo = coo_data
        num_items = shape[1]
        test_matrix = csr_matrix((fdbk_coo, (user_coo, item_coo)),
                                  shape=(num_users, num_items),
                                  dtype=np.float64)
        return test_matrix, coo_data


    def slice_recommendations(self, test_data, shape, start, end):
        raise NotImplementedError('This must be implemented in subclasses')


    def user_recommendations(self, i):
        test_data, test_shape = self._get_test_data()
        scores, seen_idx = self.slice_recommendations(test_data, test_shape, i, i+1)
        return scores.squeeze(), seen_idx[1]


    def get_recommendations(self):
        if self.verify_integrity:
            self.verify_data_integrity()

        test_data, test_shape = self._get_test_data()

        topk = self.topk
        top_recs = np.empty((test_shape[0], topk), dtype=np.int64)

        user_slices = self._get_slices_idx(test_shape)
        start = user_slices[0]
        for i in user_slices[1:]:
            stop = i
            scores, slice_data = self.slice_recommendations(test_data, test_shape, start, stop)

            if self.filter_seen:
                #prevent seen items from appearing in recommendations
                self.downvote_seen_items(scores, slice_data)

            top_recs[start:stop, :] = self.get_topk_items(scores)
            start = stop

        return top_recs


    def get_matched_predictions(self):
        userid, itemid = self.data.fields.userid, self.data.fields.itemid
        holdout_data = self.data.test.evalset[itemid]
        holdout = self.data.holdout_size
        holdout_matrix = holdout_data.values.reshape(-1, holdout).astype(np.int64)

        recommendations = self.recommendations #will recalculate if empty

        if recommendations.shape[0] > holdout_matrix.shape[0]:
            print 'Evaluation set is truncated.'
            recommendations = recommendations[:holdout_matrix.shape[0], :]
        elif recommendations.shape[0] < holdout_matrix.shape[0]:
            print 'Recommendations are truncated.'
            holdout_matrix = holdout_matrix[:recommendations.shape[0], :]

        matched_predictions = (recommendations[:, :, None] == holdout_matrix[:, None, :])
        return matched_predictions


    def get_feedback_data(self, on_level=None):
        feedback = self.data.fields.feedback
        eval_data = self.data.test.evalset[feedback].values
        holdout = self.data.holdout_size
        feedback_data = eval_data.reshape(-1, holdout)

        if on_level is not None:
            try:
                iter(on_level)
                mask_level = np.in1d(feedback_data.ravel(),
                                     on_level,
                                     invert=True).reshape(feedback_data.shape)
                feedback_data = np.ma.masked_where(mask_level, feedback_data)
            except TypeError:
                feedback_data = np.ma.masked_not_equal(feedback_data, on_level)
        return feedback_data


    def get_positive_feedback(self, on_level=None):
        feedback_data = self.get_feedback_data(on_level)
        positive_feedback = feedback_data >= self.switch_positive
        return positive_feedback


    def evaluate(self, method='hits', topk=None, on_feedback_level=None):
        #support rolling back scenario for @k calculations
        if topk > self.topk:
            self.topk = topk #will also empty flush old recommendations

        matched_predictions = self.get_matched_predictions()
        matched_predictions = matched_predictions[:, :topk, :]

        if method == 'relevance':
            positive_feedback = self.get_positive_feedback(on_feedback_level)
            scores = get_relevance_scores(matched_predictions, positive_feedback)
        elif method == 'ranking':
            feedback = self.get_feedback_data(on_feedback_level)
            scores = get_ranking_scores(matched_predictions, feedback, self.switch_positive)
        elif method == 'hits':
            positive_feedback = self.get_positive_feedback(on_feedback_level)
            scores = get_hits(matched_predictions, positive_feedback)
        else:
            raise NotImplementedError
        return scores


    @staticmethod
    def topsort(a, topk):
        parted = np.argpartition(a, -topk)[-topk:]
        return parted[np.argsort(-a[parted])]


    @staticmethod
    def downvote_seen_items(recs, idx_seen):
        # NOTE for sparse scores matrix this method can lead to a slightly worse
        # results (comparing to the same method but with "densified" scores matrix)
        # models with sparse scores can alleviate that by extending recommendations
        # list with most popular items or items generated by a more sophisticated logic
        idx_seen = idx_seen[:2] # need only users and items
        if sp.sparse.issparse(recs):
            # No need to create 2 idx sets form idx lists.
            # When creating a set have to iterate over list (O(n)).
            # Intersecting set with list gives the same O(n).
            # So there's no performance gain in converting large list into set!
            # Moreover, large set creates additional memory overhead. Hence,
            # need only to create set from the test idx and calc intersection.
            recs_idx = pd.lib.fast_zip(list(recs.nonzero())) #larger
            seen_idx = pd.lib.fast_zip(list(idx_seen)) #smaller
            idx_seen_bool = np.in1d(recs_idx, set(seen_idx))
            # sparse data may have no intersections with seen items
            if idx_seen_bool.any():
                seen_data = recs.data[idx_seen_bool]
                # move seen items scores below minimum value
                # if not enough data, seen items won't be filtered out
                lowered = recs.data.min() - (seen_data.max() - seen_data) - 1
                recs.data[idx_seen_bool] = lowered
        else:
            idx_seen_flat = np.ravel_multi_index(idx_seen, recs.shape)
            seen_data = recs.flat[idx_seen_flat]
            # move seen items scores below minimum value
            lowered = recs.min() - (seen_data.max() - seen_data) - 1
            recs.flat[idx_seen_flat] = lowered


    def get_topk_items(self, scores):
        topk = self.topk
        if sp.sparse.issparse(scores):
            # there can be less then topk values in some rows
            # need to extend sorted scores to conform with evaluation matrix shape
            # can do this by adding -1's to the right, however:
            # this relies on the fact that there are no -1's in evaluation matrix
            # NOTE need to ensure that this is always true
            def topscore(x, k):
                data = x.data.values
                cols = x.cols.values
                nnz = len(data)
                if k >= nnz:
                    cols_sorted = cols[np.argsort(-data)]
                    # need to pad values to conform with evaluation matrix shape
                    res = np.pad(cols_sorted, (0, k-nnz), 'constant', constant_values=self._pad_const)
                else:
                    # TODO verify, that even if k is relatively small, then
                    # argpartition doesn't add too much overhead?
                    res = cols[self.topsort(data, k)]
                return res

            idx = scores.nonzero()
            row_data = pd.DataFrame({'data': scores.data, 'cols': idx[1]}).groupby(idx[0], sort=True)
            recs = np.asarray(row_data.apply(topscore, topk).tolist())
        else:
        # apply_along_axis is more memory efficient then argsort on full array
            recs = np.apply_along_axis(self.topsort, 1, scores, topk)
        return recs


    @staticmethod
    def orthogonalize(u, v):
        Qu, Ru = np.linalg.qr(u)
        Qv, Rv = np.linalg.qr(v)
        Ur, Sr, Vr = np.linalg.svd(Ru.dot(Rv.T))
        U = Qu.dot(Ur)
        V = Qv.dot(Vr.T)
        return U, V


    def verify_data_integrity(self):
        data = self.data
        userid, itemid, feedback = data.fields

        nunique_items = data.training[itemid].nunique()
        nunique_test_users = data.test.testset[userid].nunique()

        assert nunique_items == len(data.index.itemid)
        assert nunique_items == data.training[itemid].max() + 1
        assert nunique_test_users == data.test.testset[userid].max() + 1

        try:
            assert self._items_factors.shape[0] == len(data.index.itemid)
            assert self._feedback_factors.shape[0] == len(data.index.feedback)
        except AttributeError:
            pass


class NonPersonalized(RecommenderModel):

    def __init__(self, kind, *args, **kwargs):
        super(NonPersonalized, self).__init__(*args, **kwargs)
        self.method = kind


    def build(self):
        self._recommendations = None


    def get_recommendations(self):
        userid, itemid, feedback = self.data.fields
        test_data = self.data.test.testset
        test_idx = (test_data[userid].values.astype(np.int64),
                    test_data[itemid].values.astype(np.int64))
        num_users = self.data.test.testset[userid].max() + 1

        if self.method == 'mostpopular':
            items_scores = self.data.training.groupby(itemid, sort=True).size().values
            #scores =  np.lib.stride_tricks.as_strided(items_scores, (num_users, items_scores.size), (0, items_scores.itemsize))
            scores = np.repeat(items_scores[None, :], num_users, axis=0)
        elif self.method == 'random':
            num_items = self.data.training[itemid].max() + 1
            scores = np.random.random((num_users, num_items))
        elif self.method == 'topscore':
            items_scores = self.data.training.groupby(itemid, sort=True)[feedback].sum().values
            scores = np.repeat(items_scores[None, :], num_users, axis=0)
        else:
            raise NotImplementedError

        if self.filter_seen:
            #prevent seen items from appearing in recommendations
            self.downvote_seen_items(scores, test_idx)

        top_recs =  self.get_topk_items(scores)
        return top_recs


class CooccurrenceModel(RecommenderModel):

    def __init__(self, *args, **kwargs):
        super(CooccurrenceModel, self).__init__(*args, **kwargs)
        self.method = 'item-to-item' #pick some meaningful name
        self.implicit = True


    def build(self):
        self._recommendations = None
        idx, val, shp = self.data.to_coo()

        if self.implicit:
            val = np.ones_like(val)

        user_item_matrix = csr_matrix((val, (idx[:, 0], idx[:, 1])),
                                        shape=shp, dtype=np.float64)
        tik = timer()
        i2i_matrix = user_item_matrix.T.dot(user_item_matrix)

        #exclude "self-links"
        diag_vals = i2i_matrix.diagonal()
        i2i_matrix -= sp.sparse.dia_matrix((diag_vals, 0), shape=i2i_matrix.shape)
        tok = timer() - tik
        print '{} model training time: {}s'.format(self.method, tok)

        self._i2i_matrix = i2i_matrix


    def get_recommendations(self):
        test_data = self.data.test_to_coo()
        test_shape = self.data.get_test_shape()
        test_matrix, _ = self.get_test_matrix(test_data, test_shape)
        if self.implicit:
            test_matrix.data = np.ones_like(test_matrix.data)

        i2i_scores = test_matrix.dot(self._i2i_matrix)

        if self.filter_seen:
            # prevent seen items from appearing in recommendations;
            # caution: there's a risk of having seen items in the list
            # (for topk < i2i_matrix.shape[1]-len(unseen))
            # this is related to low generalization ability
            # of the naive cooccurrence method itself, not to the algorithm
            self.downvote_seen_items(i2i_scores, test_data)

        top_recs = self.get_topk_items(i2i_scores)
        return top_recs


class SVDModel(RecommenderModel):

    def __init__(self, *args, **kwargs):
        super(SVDModel, self).__init__(*args, **kwargs)
        self.rank = defaults.svd_rank
        self.method = 'SVD'


    def build(self):
        self._recommendations = None
        idx, val, shp = self.data.to_coo(tensor_mode=False)
        svd_matrix = csr_matrix((val, (idx[:, 0], idx[:, 1])),
                                shape=shp, dtype=np.float64)

        tik = timer()
        _, _, items_factors = svds(svd_matrix, k=self.rank, return_singular_vectors='vh')
        tok = timer() - tik
        print '{} model training time: {}s'.format(self.method, tok)

        self._items_factors = np.ascontiguousarray(items_factors[::-1, :]).T


    def slice_recommendations(self, test_data, shape, start, stop):
        test_matrix, slice_data = self.get_test_matrix(test_data, shape, (start, stop))
        v = self._items_factors
        scores = (test_matrix.dot(v)).dot(v.T)
        return scores, slice_data


class CoffeeModel(RecommenderModel):

    def __init__(self, *args, **kwargs):
        super(CoffeeModel, self).__init__(*args, **kwargs)
        self.mlrank = defaults.mlrank
        self.chunk = defaults.test_chunk_size
        self.method = 'CoFFee'
        self._flattener = defaults.flattener
        self.growth_tol = defaults.growth_tol
        self.num_iters = defaults.num_iters
        self.show_output = defaults.show_output


    @property
    def flattener(self):
        return self._flattener

    @flattener.setter
    def flattener(self, new_value):
        old_value = self._flattener
        if new_value != old_value:
            self._flattener = new_value
            self._recommendations = None


    @staticmethod
    def flatten_scores(tensor_scores, flattener=None):
        flattener = flattener or slice(None)
        if isinstance(flattener, str):
            slicer = slice(None)
            flatten = getattr(np, flattener)
            matrix_scores = flatten(tensor_scores[:, :, slicer], axis=-1)
        elif isinstance(flattener, int):
            slicer = flattener
            matrix_scores = tensor_scores[:, :, slicer]
        elif isinstance(flattener, list) or isinstance(flattener, slice):
            slicer = flattener
            flatten = np.sum
            matrix_scores = flatten(tensor_scores[:, :, slicer], axis=-1)
        elif isinstance(flattener, tuple):
            slicer, flatten_method = flattener
            slicer = slicer or slice(None)
            flatten = getattr(np, flatten_method)
            matrix_scores = flatten(tensor_scores[:, :, slicer], axis=-1)
        elif callable(flattener):
            matrix_scores = flattener(tensor_scores)
        else:
            raise ValueError('Unrecognized value for flattener attribute')
        return matrix_scores


    def build(self):
        self._recommendations = None
        idx, val, shp = self.data.to_coo(tensor_mode=True)
        tik = timer()
        users_factors, items_factors, feedback_factors, core = \
                            tucker_als(idx, val, shp, self.mlrank,
                            growth_tol=self.growth_tol,
                            iters = self.num_iters,
                            batch_run=not self.show_output)
        tok = timer() - tik
        print '{} model training time: {}s'.format(self.method, tok)
        self._users_factors = users_factors
        self._items_factors = items_factors
        self._feedback_factors = feedback_factors
        self._core = core


    def get_test_tensor(self, test_data, shape, start, end):
        slice_idx = self._slice_test_data(test_data, start, end)

        num_users = end - start
        num_items = shape[1]
        num_fdbks = shape[2]
        slice_shp = (num_users, num_items, num_fdbks)

        idx_flat = np.ravel_multi_index(slice_idx, slice_shp)
        shp_flat = (num_users*num_items, num_fdbks)
        idx = np.unravel_index(idx_flat, shp_flat)
        val = np.ones_like(slice_idx[2])

        test_tensor_unfolded = csr_matrix((val, idx), shape=shp_flat, dtype=val.dtype)
        return test_tensor_unfolded, slice_idx


    def slice_recommendations(self, test_data, shape, start, end):
        test_tensor_unfolded, slice_idx = self.get_test_tensor(test_data, shape, start, end)
        num_users = end - start
        num_items = shape[1]
        num_fdbks = shape[2]
        v = self._items_factors
        w = self._feedback_factors

        # assume that w.shape[1] < v.shape[1] (allows for more efficient calculations)
        scores = test_tensor_unfolded.dot(w).reshape(num_users, num_items, w.shape[1])
        scores = np.tensordot(scores, v, axes=(1, 0))
        scores = np.tensordot(np.tensordot(scores, v, axes=(2, 1)), w, axes=(1, 1))
        scores = self.flatten_scores(scores, self.flattener)
        return scores, slice_idx

    # additional functionality: rating pediction
    def get_holdout_slice(self, start, stop):
        userid = self.data.fields.userid
        itemid = self.data.fields.itemid
        eval_data = self.data.test.evalset

        user_sel = (eval_data[userid] >= start) & (eval_data[userid] < stop)
        holdout_users = eval_data.loc[user_sel, userid].values.astype(np.int64) - start
        holdout_items = eval_data.loc[user_sel, itemid].values.astype(np.int64)
        return (holdout_users, holdout_items)


    def predict_feedback(self):
        flattener_old = self.flattener
        self.flattener = 'argmax' #this will be applied along feedback axis
        feedback_idx = self.data.index.feedback.set_index('new')

        test_data, test_shape = self._get_test_data()
        holdout_size = self.data.holdout_size
        dtype = feedback_idx.old.dtype
        predicted_feedback = np.empty((test_shape[0], holdout_size), dtype=dtype)

        user_slices = self._get_slices_idx(test_shape, result_width=holdout_size)
        start = user_slices[0]
        for i in user_slices[1:]:
            stop = i
            predicted, _ = self.slice_recommendations(test_data, test_shape, start, stop)
            holdout_idx = self.get_holdout_slice(start, stop)
            feedback_values = feedback_idx.loc[predicted[holdout_idx], 'old'].values
            predicted_feedback[start:stop, :] = feedback_values.reshape(-1, holdout_size)
            start = stop
        self.flattener = flattener_old
        return predicted_feedback
