# Copyright 2018 Changan Wang

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================
import math

import tensorflow as tf
import numpy as np

from tensorflow.contrib.image.python.ops import image_ops

def areas(bboxes):
    ymin, xmin, ymax, xmax = tf.split(bboxes, 4, axis=1)
    return (xmax - xmin) * (ymax - ymin)
def intersection(bboxes, gt_bboxes):
    ymin, xmin, ymax, xmax = tf.split(bboxes, 4, axis=1)
    gt_ymin, gt_xmin, gt_ymax, gt_xmax = [tf.transpose(b, perm=[1, 0]) for b in tf.split(gt_bboxes, 4, axis=1)]

    int_ymin = tf.maximum(ymin, gt_ymin)
    int_xmin = tf.maximum(xmin, gt_xmin)
    int_ymax = tf.minimum(ymax, gt_ymax)
    int_xmax = tf.minimum(xmax, gt_xmax)
    h = tf.maximum(int_ymax - int_ymin, 0.)
    w = tf.maximum(int_xmax - int_xmin, 0.)

    return h * w
def iou_matrix(bboxes, gt_bboxes):
    inter_vol = intersection(bboxes, gt_bboxes)
    union_vol = areas(bboxes) + tf.transpose(areas(gt_bboxes), perm=[1, 0]) - inter_vol

    return tf.where(tf.equal(inter_vol, 0.0),
                    tf.zeros_like(inter_vol), tf.truediv(inter_vol, union_vol))

def do_dual_max_match(overlap_matrix, high_thres, low_thres, ignore_between = True, gt_max_first=True):
    '''
    overlap_matrix: num_gt * num_anchors
    '''
    anchors_to_gt = tf.argmax(overlap_matrix, axis=0)
    match_values = tf.reduce_max(overlap_matrix, axis=0)

    positive_mask = tf.greater_equal(match_values, high_thres)
    less_mask = tf.less(match_values, low_thres)
    between_mask = tf.logical_and(tf.less(match_values, high_thres), tf.greater_equal(match_values, low_thres))
    negative_mask = less_mask if ignore_between else between_mask
    ignore_mask = between_mask if ignore_between else less_mask

    match_indices = tf.where(negative_mask, -1 * tf.ones_like(anchors_to_gt), anchors_to_gt)
    match_indices = tf.where(ignore_mask, -2 * tf.ones_like(match_indices), match_indices)

    anchors_to_gt_mask = tf.one_hot(tf.clip_by_value(match_indices, -1, tf.cast(tf.shape(overlap_matrix)[0], tf.int64)), tf.shape(overlap_matrix)[0], on_value=1, off_value=0, axis=0, dtype=tf.int32)

    gt_to_anchors = tf.argmax(overlap_matrix, axis=1)

    if gt_max_first:
        left_gt_to_anchors_mask = tf.one_hot(gt_to_anchors, tf.shape(overlap_matrix)[1], on_value=1, off_value=0, axis=1, dtype=tf.int32)
    else:
        left_gt_to_anchors_mask = tf.cast(tf.logical_and(tf.reduce_max(anchors_to_gt_mask, axis=1, keep_dims=True) < 1, tf.one_hot(gt_to_anchors, tf.shape(overlap_matrix)[1], on_value=True, off_value=False, axis=1, dtype=tf.bool)), tf.int64)

    selected_scores = tf.gather_nd(overlap_matrix, tf.stack([tf.where(tf.reduce_max(left_gt_to_anchors_mask, axis=0) > 0, tf.argmax(left_gt_to_anchors_mask, axis=0), anchors_to_gt), tf.range(tf.cast(tf.shape(overlap_matrix)[1], tf.int64))], axis=1))
    return tf.where(tf.reduce_max(left_gt_to_anchors_mask, axis=0) > 0, tf.argmax(left_gt_to_anchors_mask, axis=0), match_indices), selected_scores

class AnchorEncoder(object):
    def __init__(self, anchors, num_classes, allowed_borders, positive_threshold, ignore_threshold, prior_scaling, rpn_fg_thres = 0.5, rpn_bg_high_thres = 0.5, rpn_bg_low_thres = 0.):
        super(AnchorEncoder, self).__init__()
        self._labels = None
        self._bboxes = None
        self._anchors = anchors
        self._num_classes = num_classes
        self._allowed_borders = allowed_borders
        self._positive_threshold = positive_threshold
        self._ignore_threshold = ignore_threshold
        self._prior_scaling = prior_scaling
        self._rpn_fg_thres = rpn_fg_thres
        self._rpn_bg_high_thres = rpn_bg_high_thres
        self._rpn_bg_low_thres = rpn_bg_low_thres

    def center2point(self, center_y, center_x, height, width):
        return center_y - height / 2., center_x - width / 2., center_y + height / 2., center_x + width / 2.,

    def point2center(self, ymin, xmin, ymax, xmax):
        height, width = (ymax - ymin), (xmax - xmin)
        return ymin + height / 2., xmin + width / 2., height, width

    def encode_anchor(self, anchor, allowed_border):
        assert self._labels is not None, 'must provide labels to encode anchors.'
        assert self._bboxes is not None, 'must provide bboxes to encode anchors.'
        # y, x, h, w are all in range [0, 1] relative to the original image size
        yref, xref, href, wref = tf.expand_dims(anchor[0], axis=-1), tf.expand_dims(anchor[1], axis=-1), anchor[2], anchor[3]
        # for the shape of ymin, xmin, ymax, xmax
        # [[[anchor_0, anchor_1, anchor_2, ...], [anchor_0, anchor_1, anchor_2, ...], [anchor_0, anchor_1, anchor_2, ...], ...],
        # [[anchor_0, anchor_1, anchor_2, ...], [anchor_0, anchor_1, anchor_2, ...], [anchor_0, anchor_1, anchor_2, ...], ...],
        #                                   .
        #                                   .
        # [[anchor_0, anchor_1, anchor_2, ...], [anchor_0, anchor_1, anchor_2, ...], [anchor_0, anchor_1, anchor_2, ...], ...]]
        ymin_, xmin_, ymax_, xmax_ = self.center2point(yref, xref, href, wref)

        ymin, xmin, ymax, xmax = tf.reshape(ymin_, [-1]), tf.reshape(xmin_, [-1]), tf.reshape(ymax_, [-1]), tf.reshape(xmax_, [-1])
        anchors_point = tf.stack([ymin, xmin, ymax, xmax], axis=-1)

        #anchors_point = tf.Print(anchors_point,[tf.shape(anchors_point)])
        inside_mask = tf.logical_and(tf.logical_and(ymin >= -allowed_border*1., xmin >= -allowed_border*1.),
                                                                tf.logical_and(ymax < (1. + allowed_border*1.), xmax < (1. + allowed_border*1.)))

        overlap_matrix = iou_matrix(self._bboxes, anchors_point) * tf.cast(tf.expand_dims(inside_mask, 0), tf.float32)
        #overlap_matrix = tf.Print(overlap_matrix, [tf.shape(overlap_matrix)], message='overlap_matrix: ', summarize=1000)
        matched_gt, gt_scores = do_dual_max_match(overlap_matrix, self._positive_threshold, self._ignore_threshold)

        matched_gt_mask = matched_gt > -1
        #matched_gt = tf.Print(matched_gt,[matched_gt], message='matched_gt: ', summarize=1000)
        matched_indices = tf.clip_by_value(matched_gt, 0, tf.int64.max)
        gt_labels = tf.gather(self._labels, matched_indices)
        #gt_labels = tf.Print(gt_labels,[gt_labels, tf.count_nonzero(gt_labels * tf.cast(matched_gt_mask, tf.int64) + (-1 * tf.cast(matched_gt < -1, tf.int64))>0), gt_labels * tf.cast(matched_gt_mask, tf.int64) + (-1 * tf.cast(matched_gt < -1, tf.int64))], message='gt_labels: ', summarize=1000)
        #gt_labels = tf.Print(gt_labels,[tf.shape(ymin_)], message='gt_labels: ', summarize=1000)
        gt_ymin, gt_xmin, gt_ymax, gt_xmax = [tf.reshape(b, tf.shape(ymin_)) for b in tf.split(tf.gather(self._bboxes, matched_indices), 4, axis=1)]

        # Transform to center / size.
        gt_cy = (gt_ymax + gt_ymin) / 2.
        gt_cx = (gt_xmax + gt_xmin) / 2.
        gt_h = gt_ymax - gt_ymin
        gt_w = gt_xmax - gt_xmin

        # Encode features.
        # the prior_scaling (in fact is 5 and 10) is use for balance the regression loss of center and with(or height)
        # (x-x_ref)/x_ref * 10 + log(w/w_ref) * 5
        gt_cy = (gt_cy - yref) / href / self._prior_scaling[0]
        gt_cx = (gt_cx - xref) / wref / self._prior_scaling[1]
        gt_h = tf.log(gt_h / href) / self._prior_scaling[2]
        gt_w = tf.log(gt_w / wref) / self._prior_scaling[3]
        # Use SSD ordering: x / y / w / h instead of ours.
        gt_localizations = tf.stack([gt_cy, gt_cx, gt_h, gt_w], axis=-1)
        # now gt_localizations is our regression object

        return gt_labels * tf.cast(matched_gt_mask, tf.int64) + (-1 * tf.cast(matched_gt < -1, tf.int64)), \
                tf.expand_dims(tf.reshape(tf.cast(matched_gt_mask, tf.float32), \
                                            tf.shape(ymin_)), -1) * gt_localizations, \
                gt_scores, \
                tf.stack([ymin, xmin, ymax, xmax], axis=-1)
    # def encode_anchor(self, anchor, allowed_border):
    #     assert self._labels is not None, 'must provide labels to encode anchors.'
    #     assert self._bboxes is not None, 'must provide bboxes to encode anchors.'
    #     # y, x, h, w are all in range [0, 1] relative to the original image size
    #     yref, xref, href, wref = tf.expand_dims(anchor[0], axis=-1), tf.expand_dims(anchor[1], axis=-1), anchor[2], anchor[3]
    #     # for the shape of ymin, xmin, ymax, xmax
    #     # [[[anchor_0, anchor_1, anchor_2, ...], [anchor_0, anchor_1, anchor_2, ...], [anchor_0, anchor_1, anchor_2, ...], ...],
    #     # [[anchor_0, anchor_1, anchor_2, ...], [anchor_0, anchor_1, anchor_2, ...], [anchor_0, anchor_1, anchor_2, ...], ...],
    #     #                                   .
    #     #                                   .
    #     # [[anchor_0, anchor_1, anchor_2, ...], [anchor_0, anchor_1, anchor_2, ...], [anchor_0, anchor_1, anchor_2, ...], ...]]
    #     ymin_, xmin_, ymax_, xmax_ = self.center2point(yref, xref, href, wref)

    #     vol_anchors = (xmax - xmin) * (ymax - ymin)

    #     inside_mask = tf.logical_and(tf.logical_and(ymin >= -allowed_border*1., xmin >= -allowed_border*1.),
    #                                                             tf.logical_and(ymax < (1. + allowed_border*1.), xmax < (1. + allowed_border*1.)))

    #     # store every jaccard score while loop all ground truth, will update depends the score of anchor and current ground_truth
    #     gt_labels = tf.zeros_like(ymin, dtype=tf.int64)
    #     gt_scores = tf.zeros_like(ymin, dtype=tf.float32)

    #     gt_ymin = tf.zeros_like(ymin, dtype=tf.float32)
    #     gt_xmin = tf.zeros_like(ymin, dtype=tf.float32)
    #     gt_ymax = tf.ones_like(ymin, dtype=tf.float32)
    #     gt_xmax = tf.ones_like(ymin, dtype=tf.float32)

    #     max_mask = tf.cast(tf.zeros_like(ymin, dtype=tf.int32), tf.bool)

    #     def safe_divide(numerator, denominator):
    #         return tf.where(
    #             tf.greater(denominator, 0),
    #             tf.divide(numerator, denominator),
    #             tf.zeros_like(denominator))

    #     def jaccard_with_anchors(bbox):
    #         """Compute jaccard score between a box and the anchors.
    #         """
    #         # the inner square
    #         inner_ymin = tf.maximum(ymin, bbox[0])
    #         inner_xmin = tf.maximum(xmin, bbox[1])
    #         inner_ymax = tf.minimum(ymax, bbox[2])
    #         inner_xmax = tf.minimum(xmax, bbox[3])
    #         h = tf.maximum(inner_ymax - inner_ymin, 0.)
    #         w = tf.maximum(inner_xmax - inner_xmin, 0.)

    #         inner_vol = h * w
    #         union_vol = vol_anchors - inner_vol \
    #             + (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    #         jaccard = safe_divide(inner_vol, union_vol)
    #         return jaccard

    #     def condition(i, gt_labels, gt_scores,
    #                   gt_ymin, gt_xmin, gt_ymax, gt_xmax, max_mask):
    #         return tf.less(i, tf.shape(self._labels))[0]

    #     def body(i, gt_labels, gt_scores,
    #              gt_ymin, gt_xmin, gt_ymax, gt_xmax, max_mask):
    #         """Body: update gture labels, scores and bboxes.
    #         Follow the original SSD paper for that purpose:
    #           - assign values when jaccard > 0.5;
    #           - only update if beat the score of other bboxes.
    #         """
    #         # get i_th groud_truth(label && bbox)
    #         label = self._labels[i]
    #         bbox = self._bboxes[i]
    #         # # current ground_truth's overlap with all others' anchors
    #         # jaccard = tf.cast(inside_mask, tf.float32) * jaccard_with_anchors(bbox)
    #         # #jaccard = jaccard_with_anchors(bbox)
    #         # # the index of the max overlap for current ground_truth
    #         # max_jaccard = tf.reduce_max(jaccard)
    #         # cur_max_indice_mask = tf.equal(jaccard, max_jaccard)

    #         # current ground_truth's overlap with all others' anchors
    #         jaccard = tf.cast(inside_mask, tf.float32) * jaccard_with_anchors(bbox)
    #         # the index of the max overlap for current ground_truth
    #         max_jaccard = tf.maximum(tf.reduce_max(jaccard), ignore_threshold)
    #         #max_jaccard = tf.Print(max_jaccard, [max_jaccard], message='max_jaccard: ', summarize=500)
    #         all_cur_max_indice_mask = tf.equal(jaccard, max_jaccard)

    #         choice_jaccard = tf.cast(all_cur_max_indice_mask, tf.float32) * jaccard * tf.random_uniform(tf.shape(all_cur_max_indice_mask), minval=1., maxval=10.)

    #         max_choice_jaccard = tf.maximum(tf.reduce_max(choice_jaccard), ignore_threshold)
    #         cur_max_indice_mask = tf.equal(choice_jaccard, max_choice_jaccard)

    #         # the locations where current overlap is higher than before
    #         greater_than_current_mask = tf.greater(jaccard, gt_scores)
    #         # we will update these locations as well as the current max_overlap location for this ground_truth
    #         locations_to_update = tf.logical_or(greater_than_current_mask, cur_max_indice_mask)
    #         # but we will ignore those locations where is the max_overlap for any ground_truth before
    #         locations_to_update_with_mask = tf.logical_and(locations_to_update, tf.logical_not(max_mask))
    #         # for current max_overlap
    #         # for those current overlap is higher than before
    #         # for those locations where is not the max_overlap for any before ground_truth
    #         # update scores, so the terminal scores are either those max_overlap along the way or the max_overlap for any ground_truth
    #         gt_scores = tf.where(locations_to_update_with_mask, jaccard, gt_scores)

    #         # !!! because the difference of rules for score and label update !!!
    #         # !!! so before we get the negtive examples we must use labels as positive mask first !!!
    #         # for current max_overlap
    #         # for current jaccard higher than before and higher than threshold (those scores are lower than is is ignored)
    #         # for those locations where is not the max_overlap for any before ground_truth
    #         # update labels, so the terminal labels are either those with max_overlap and higher than threshold along the way or the max_overlap for any ground_truth
    #         # locations_to_update_labels = tf.logical_or(tf.greater(tf.cast(greater_than_current_mask, tf.float32) * jaccard, self._ignore_threshold), cur_max_indice_mask)
    #         locations_to_update_labels = tf.logical_and(tf.logical_or(tf.greater(tf.cast(greater_than_current_mask, tf.float32) * jaccard, self._ignore_threshold), cur_max_indice_mask), tf.logical_not(max_mask))
    #         locations_to_update_labels_mask = tf.cast(tf.logical_and(locations_to_update_labels, label < self._num_classes), tf.float32)

    #         gt_labels = tf.cast(locations_to_update_labels_mask, tf.int64) * label + (1 - tf.cast(locations_to_update_labels_mask, tf.int64)) * gt_labels
    #         #gt_scores = tf.where(mask, jaccard, gt_scores)
    #         # update ground truth for each anchors depends on the mask
    #         gt_ymin = locations_to_update_labels_mask * bbox[0] + (1 - locations_to_update_labels_mask) * gt_ymin
    #         gt_xmin = locations_to_update_labels_mask * bbox[1] + (1 - locations_to_update_labels_mask) * gt_xmin
    #         gt_ymax = locations_to_update_labels_mask * bbox[2] + (1 - locations_to_update_labels_mask) * gt_ymax
    #         gt_xmax = locations_to_update_labels_mask * bbox[3] + (1 - locations_to_update_labels_mask) * gt_xmax

    #         # update max_mask along the way
    #         max_mask = tf.logical_or(max_mask, cur_max_indice_mask)

    #         return [i+1, gt_labels, gt_scores,
    #                 gt_ymin, gt_xmin, gt_ymax, gt_xmax, max_mask]
    #     # Main loop definition.
    #     # iterate betwween all ground_truth to encode anchors
    #     i = 0
    #     [i, gt_labels, gt_scores,
    #      gt_ymin, gt_xmin,
    #      gt_ymax, gt_xmax, max_mask] = tf.while_loop(condition, body,
    #                                            [i, gt_labels, gt_scores,
    #                                             gt_ymin, gt_xmin,
    #                                             gt_ymax, gt_xmax, max_mask], parallel_iterations=16, back_prop=False, swap_memory=True)
    #     # give -1 to the label of anchors those are outside image
    #     inside_int_mask = tf.cast(inside_mask, tf.int64)
    #     gt_labels =  (1 - inside_int_mask) * -1 + inside_int_mask * gt_labels
    #     # transform to center / size for later regression target calculating
    #     gt_cy = (gt_ymax + gt_ymin) / 2.
    #     gt_cx = (gt_xmax + gt_xmin) / 2.
    #     gt_h = gt_ymax - gt_ymin
    #     gt_w = gt_xmax - gt_xmin
    #     # get regression target for smooth_l1_loss
    #     # the prior_scaling (in fact is 5 and 10) is use for balance the regression loss of center and with(or height)
    #     # (x-x_ref)/x_ref * 10 + log(w/w_ref) * 5
    #     gt_cy = (gt_cy - yref) / href / self._prior_scaling[0]
    #     gt_cx = (gt_cx - xref) / wref / self._prior_scaling[1]
    #     gt_h = tf.log(gt_h / href) / self._prior_scaling[2]
    #     gt_w = tf.log(gt_w / wref) / self._prior_scaling[3]

    #     # now gt_localizations is our regression object
    #     return gt_labels, tf.stack([gt_cy, gt_cx, gt_h, gt_w], axis=-1), gt_scores
    def encode_all_anchors(self, labels, bboxes):
        self._labels = labels
        self._bboxes = bboxes

        ground_labels = []
        anchor_regress_targets = []
        ground_scores = []
        ground_bboxes = []

        for layer_index, anchor in enumerate(self._anchors):
            ground_label, anchor_regress_target, ground_score, ground_bbox = self.encode_anchor(anchor, self._allowed_borders[layer_index])
            ground_labels.append(ground_label)
            anchor_regress_targets.append(anchor_regress_target)
            ground_scores.append(ground_score)
            ground_bboxes.append(ground_bbox)
        #return ground_labels, anchor_regress_targets, ground_scores, len(self._anchors)
        return ground_labels, anchor_regress_targets, ground_scores, ground_bboxes, len(self._anchors)

    def ext_encode_rois(self, all_rois, all_labels, all_bboxes, rois_per_image, fg_fraction, allowed_border, head_prior_scaling=[1., 1., 1., 1.]):
        '''Do encoder for rois from SS or RPN
        fg_fraction: the fraction of fg in total bboxes
        '''

        #all_rois = tf.Print(all_rois, [all_rois], message='all_rois:')
        expected_num_fg_rois = tf.cast(tf.round(tf.cast(rois_per_image, tf.float32) * fg_fraction), tf.int32)
        #expected_num_bg_rois = rois_per_image - expected_num_fg_rois
        def encode_impl(_rois, _labels, _bboxes):
            '''encode along batch
            '''
            _bboxes = tf.boolean_mask(_bboxes, _labels > 0)
            _labels = tf.boolean_mask(_labels, _labels > 0)
            #print(_labels)
            # we should first include all ground truth, then we match them all together
            _rois = tf.concat([_rois, _bboxes], axis = 0)

            ymin_, xmin_, ymax_, xmax_ = _rois[:, 0], _rois[:, 1], _rois[:, 2], _rois[:, 3]

            ymin, xmin, ymax, xmax = tf.reshape(ymin_, [-1]), tf.reshape(xmin_, [-1]), tf.reshape(ymax_, [-1]), tf.reshape(xmax_, [-1])
            anchors_point = tf.stack([ymin, xmin, ymax, xmax], axis=-1)

            inside_mask = tf.logical_and(tf.logical_and(ymin >= -allowed_border*1., xmin >= -allowed_border*1.),
                                                                    tf.logical_and(ymax < (1. + allowed_border*1.), xmax < (1. + allowed_border*1.)))

            overlap_matrix = iou_matrix(_bboxes, anchors_point) * tf.cast(tf.expand_dims(inside_mask, 0), tf.float32)


            matched_gt, gt_scores = do_dual_max_match(overlap_matrix, self._rpn_fg_thres, self._rpn_bg_high_thres)

            matched_gt_mask = matched_gt > -1
            matched_indices = tf.clip_by_value(matched_gt, 0, tf.int64.max)

            gt_ymin, gt_xmin, gt_ymax, gt_xmax = [tf.reshape(b, tf.shape(ymin_)) for b in tf.split(tf.gather(_bboxes, matched_indices), 4, axis=1)]

            gt_labels = tf.gather(_labels, matched_indices)
            gt_labels = gt_labels * tf.cast(matched_gt_mask, tf.int64) + (-1 * tf.cast(matched_gt < -1, tf.int64))
            # transform to center / size for later regression target calculating
            gt_cy = (gt_ymax + gt_ymin) / 2.
            gt_cx = (gt_xmax + gt_xmin) / 2.
            gt_h = gt_ymax - gt_ymin
            gt_w = gt_xmax - gt_xmin

            #ymin = tf.Print(ymin, [ymin, xmin, ymax, xmax], message='ymin:')

            yref, xref, href, wref = self.point2center(ymin, xmin, ymax, xmax)
            # get regression target for smooth_l1_loss
            gt_cy = (gt_cy - yref) / href / head_prior_scaling[0]
            gt_cx = (gt_cx - xref) / wref / head_prior_scaling[1]
            gt_h = tf.log(gt_h / href) / head_prior_scaling[2]
            gt_w = tf.log(gt_w / wref) / head_prior_scaling[3]

            #gt_cy = tf.Print(gt_cy, [gt_cy, gt_cx, gt_h, gt_w], message='gt_cy:')
            # we should first include all ground truth, then we match them all together
            # total_rois = tf.concat([_rois, _bboxes], axis = 0)
            # total_targets = tf.concat([tf.expand_dims(tf.reshape(tf.cast(matched_gt_mask, tf.float32), tf.shape(ymin_)), -1) * tf.stack([gt_cy, gt_cx, gt_h, gt_w], axis=-1), tf.zeros_like(_bboxes, dtype=_bboxes.dtype)], axis = 0)

            # #_labels = tf.Print(_labels, [_labels], message='_labels:', summarize=1000)

            # total_labels = tf.concat([gt_labels, _labels], axis = 0)
            # total_scores = tf.concat([gt_scores, tf.ones_like(_labels, dtype=gt_scores.dtype)], axis = 0)

            total_rois = _rois
            total_targets = tf.expand_dims(tf.reshape(tf.cast(matched_gt_mask, tf.float32), tf.shape(ymin_)), -1) * tf.stack([gt_cy, gt_cx, gt_h, gt_w], axis=-1)
            total_labels = gt_labels
            total_scores = gt_scores

            def upsampel_impl(now_count, need_count):
                # sample with replacement
                left_count = need_count - now_count
                select_indices = tf.random_shuffle(tf.range(now_count))[:tf.floormod(left_count, now_count)]
                select_indices = tf.concat([tf.tile(tf.range(now_count), [tf.floor_div(left_count, now_count) + 1]), select_indices], axis = 0)

                return select_indices
            def downsample_impl(now_count, need_count):
                # downsample with replacement
                select_indices = tf.random_shuffle(tf.range(now_count))[:need_count]
                return select_indices
            #total_labels = tf.Print(total_labels, [total_labels], message='total_labels:', summarize=1000)

            #total_labels = tf.Print(total_labels, [tf.shape(total_labels), tf.shape(total_scores)], message='Notice Here: both the label and scores must be one vector.')

            positive_mask = total_labels > 0
            positive_indices = tf.squeeze(tf.where(positive_mask), axis = -1)
            n_positives = tf.shape(positive_indices)[0]

            #n_positives = tf.Print(n_positives, [n_positives], message='n_positives:', summarize=1000)

            # either downsample or take all
            fg_select_indices = tf.cond(n_positives < expected_num_fg_rois, lambda : positive_indices, lambda : tf.gather(positive_indices, downsample_impl(n_positives, expected_num_fg_rois)))
            # now the all rois taken as positive is min(n_positives, expected_num_fg_rois)

            #negtive_mask = tf.logical_and(tf.logical_and(tf.logical_not(tf.logical_or(positive_mask, total_labels < 0)), total_scores < self._rpn_bg_high_thres), total_scores > self._rpn_bg_low_thres)
            negtive_mask = tf.logical_and(tf.equal(total_labels, 0), total_scores > self._rpn_bg_low_thres)
            negtive_indices = tf.squeeze(tf.where(negtive_mask), axis = -1)
            n_negtives = tf.shape(negtive_indices)[0]

            expected_num_bg_rois = rois_per_image - tf.minimum(n_positives, expected_num_fg_rois)
            # either downsample or take all
            bg_select_indices = tf.cond(n_negtives < expected_num_bg_rois, lambda : negtive_indices, lambda : tf.gather(negtive_indices, downsample_impl(n_negtives, expected_num_bg_rois)))
            # now the all rois taken as positive is min(n_negtives, expected_num_bg_rois)

            keep_indices = tf.concat([fg_select_indices, bg_select_indices], axis = 0)
            n_keeps = tf.shape(keep_indices)[0]
            # now n_keeps must be equal or less than rois_per_image
            final_keep_indices = tf.cond(n_keeps < rois_per_image, lambda : tf.gather(keep_indices, upsampel_impl(n_keeps, rois_per_image)), lambda : keep_indices)

            #print(tf.gather(total_rois, final_keep_indices), tf.gather(total_targets, final_keep_indices), tf.gather(total_labels, final_keep_indices), tf.gather(total_scores, final_keep_indices))
            return tf.gather(total_rois, final_keep_indices), tf.gather(total_targets, final_keep_indices), tf.gather(total_labels, final_keep_indices), tf.gather(total_scores, final_keep_indices)
        # def encode_impl(_rois, _labels, _bboxes):
        #     '''encode along batch
        #     '''
        #     _bboxes = tf.boolean_mask(_bboxes, _labels > 0)
        #     _labels = tf.boolean_mask(_labels, _labels > 0)
        #     #print(_labels)

        #     ymin, xmin, ymax, xmax = _rois[:, 0], _rois[:, 1], _rois[:, 2], _rois[:, 3]
        #     vol_anchors = (xmax - xmin) * (ymax - ymin)
        #     # padding_maks = vol_anchors > 0
        #     # _rois, ymin, xmin, ymax, xmax = tf.boolean_mask(_rois, padding_maks), tf.boolean_mask(ymin, padding_maks), tf.boolean_mask(xmin, padding_maks), tf.boolean_mask(ymax, padding_maks), tf.boolean_mask(xmax, padding_maks)

        #     inside_mask = tf.logical_and(tf.logical_and(ymin >= -allowed_border*1., xmin >= -allowed_border*1.),
        #                                                             tf.logical_and(ymax < (1. + allowed_border*1.), xmax < (1. + allowed_border*1.)))
        #     # store every jaccard score while loop all ground truth, will update depends the score of anchor and current ground_truth
        #     gt_labels = tf.zeros_like(ymin, dtype=tf.int64)
        #     gt_scores = tf.zeros_like(ymin, dtype=tf.float32)

        #     gt_ymin = tf.zeros_like(ymin, dtype=tf.float32)
        #     gt_xmin = tf.zeros_like(ymin, dtype=tf.float32)
        #     gt_ymax = tf.ones_like(ymin, dtype=tf.float32)
        #     gt_xmax = tf.ones_like(ymin, dtype=tf.float32)

        #     max_mask = tf.cast(tf.zeros_like(ymin, dtype=tf.int32), tf.bool)

        #     def safe_divide(numerator, denominator):
        #         return tf.where(
        #             tf.greater(denominator, 0),
        #             tf.divide(numerator, denominator),
        #             tf.zeros_like(denominator))

        #     def jaccard_with_anchors(bbox):
        #         """Compute jaccard score between a box and the anchors.
        #         """
        #         # the inner square
        #         inner_ymin = tf.maximum(ymin, bbox[0])
        #         inner_xmin = tf.maximum(xmin, bbox[1])
        #         inner_ymax = tf.minimum(ymax, bbox[2])
        #         inner_xmax = tf.minimum(xmax, bbox[3])
        #         h = tf.maximum(inner_ymax - inner_ymin, 0.)
        #         w = tf.maximum(inner_xmax - inner_xmin, 0.)

        #         inner_vol = h * w
        #         union_vol = vol_anchors - inner_vol \
        #             + (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        #         jaccard = safe_divide(inner_vol, union_vol)
        #         return jaccard

        #     def condition(i, gt_labels, gt_scores,
        #                   gt_ymin, gt_xmin, gt_ymax, gt_xmax, max_mask):
        #         return tf.less(i, tf.shape(_labels)[0])

        #     def body(i, gt_labels, gt_scores,
        #              gt_ymin, gt_xmin, gt_ymax, gt_xmax, max_mask):
        #         """Body: update gture labels, scores and bboxes.
        #         Follow the original SSD paper for that purpose:
        #           - assign values when jaccard > 0.5;
        #           - only update if beat the score of other bboxes.
        #         """
        #         # get i_th groud_truth(label && bbox)
        #         label = _labels[i]
        #         bbox = _bboxes[i]

        #         # current ground_truth's overlap with all others' anchors
        #         jaccard = tf.cast(inside_mask, tf.float32) * jaccard_with_anchors(bbox)
        #         # the index of the max overlap for current ground_truth
        #         max_jaccard = tf.maximum(tf.reduce_max(jaccard), ignore_threshold)
        #         #max_jaccard = tf.Print(max_jaccard, [max_jaccard], message='max_jaccard: ', summarize=500)
        #         all_cur_max_indice_mask = tf.equal(jaccard, max_jaccard)

        #         choice_jaccard = tf.cast(all_cur_max_indice_mask, tf.float32) * jaccard * tf.random_uniform(tf.shape(all_cur_max_indice_mask), minval=1., maxval=10.)

        #         max_choice_jaccard = tf.maximum(tf.reduce_max(choice_jaccard), ignore_threshold)
        #         cur_max_indice_mask = tf.equal(choice_jaccard, max_choice_jaccard)

        #         # the locations where current overlap is higher than before
        #         greater_than_current_mask = tf.greater(jaccard, gt_scores)
        #         # we will update these locations as well as the current max_overlap location for this ground_truth
        #         locations_to_update = tf.logical_or(greater_than_current_mask, cur_max_indice_mask)
        #         # but we will ignore those locations where is the max_overlap for any ground_truth before
        #         locations_to_update_with_mask = tf.logical_and(locations_to_update, tf.logical_not(max_mask))
        #         # for current max_overlap
        #         # for those current overlap is higher than before
        #         # for those locations where is not the max_overlap for any before ground_truth
        #         # update scores, so the terminal scores are either those max_overlap along the way or the max_overlap for any ground_truth
        #         gt_scores = tf.where(locations_to_update_with_mask, jaccard, gt_scores)

        #         # !!! because the difference of rules for score and label update !!!
        #         # !!! so before we get the negtive examples we must use labels as positive mask first !!!
        #         # for current max_overlap
        #         # for current jaccard higher than before and higher than threshold (those scores are lower than is is ignored)
        #         # for those locations where is not the max_overlap for any before ground_truth
        #         # update labels, so the terminal labels are either those with max_overlap and higher than threshold along the way or the max_overlap for any ground_truth
        #         locations_to_update_labels = tf.logical_or(tf.greater(tf.cast(greater_than_current_mask, tf.float32) * jaccard, self._rpn_fg_thres), cur_max_indice_mask)
        #         # locations_to_update_labels = tf.logical_and(tf.logical_or(tf.greater(tf.cast(greater_than_current_mask, tf.float32) * jaccard, self._rpn_fg_thres), cur_max_indice_mask), tf.logical_not(max_mask))
        #         locations_to_update_labels_mask = tf.cast(tf.logical_and(locations_to_update_labels, label < self._num_classes), tf.float32)

        #         gt_labels = tf.cast(locations_to_update_labels_mask, tf.int64) * label + (1 - tf.cast(locations_to_update_labels_mask, tf.int64)) * gt_labels
        #         #gt_scores = tf.where(mask, jaccard, gt_scores)
        #         # update ground truth for each anchors depends on the mask
        #         gt_ymin = locations_to_update_labels_mask * bbox[0] + (1 - locations_to_update_labels_mask) * gt_ymin
        #         gt_xmin = locations_to_update_labels_mask * bbox[1] + (1 - locations_to_update_labels_mask) * gt_xmin
        #         gt_ymax = locations_to_update_labels_mask * bbox[2] + (1 - locations_to_update_labels_mask) * gt_ymax
        #         gt_xmax = locations_to_update_labels_mask * bbox[3] + (1 - locations_to_update_labels_mask) * gt_xmax

        #         # update max_mask along the way
        #         max_mask = tf.logical_or(max_mask, cur_max_indice_mask)

        #         return [i+1, gt_labels, gt_scores,
        #                 gt_ymin, gt_xmin, gt_ymax, gt_xmax, max_mask]
        #     # Main loop definition.
        #     # iterate betwween all ground_truth to encode anchors
        #     i = 0
        #     [i, gt_labels, gt_scores,
        #      gt_ymin, gt_xmin,
        #      gt_ymax, gt_xmax, max_mask] = tf.while_loop(condition, body,
        #                                            [i, gt_labels, gt_scores,
        #                                             gt_ymin, gt_xmin,
        #                                             gt_ymax, gt_xmax, max_mask], parallel_iterations=16, back_prop=False, swap_memory=True)
        #     # give -1 to the label of anchors those are outside image
        #     inside_int_mask = tf.cast(inside_mask, tf.int64)
        #     gt_labels =  (1 - inside_int_mask) * -1 + inside_int_mask * gt_labels
        #     # transform to center / size for later regression target calculating
        #     gt_cy = (gt_ymax + gt_ymin) / 2.
        #     gt_cx = (gt_xmax + gt_xmin) / 2.
        #     gt_h = gt_ymax - gt_ymin
        #     gt_w = gt_xmax - gt_xmin

        #     #ymin = tf.Print(ymin, [ymin, xmin, ymax, xmax], message='ymin:')

        #     yref, xref, href, wref = self.point2center(ymin, xmin, ymax, xmax)
        #     # get regression target for smooth_l1_loss
        #     gt_cy = (gt_cy - yref) / href / head_prior_scaling[0]
        #     gt_cx = (gt_cx - xref) / wref / head_prior_scaling[1]
        #     gt_h = tf.log(gt_h / href) / head_prior_scaling[2]
        #     gt_w = tf.log(gt_w / wref) / head_prior_scaling[3]

        #     #gt_cy = tf.Print(gt_cy, [gt_cy, gt_cx, gt_h, gt_w], message='gt_cy:')
        #     total_rois = tf.concat([_rois, _bboxes], axis = 0)
        #     total_targets = tf.concat([tf.stack([gt_cy, gt_cx, gt_h, gt_w], axis=-1), tf.zeros_like(_bboxes, dtype=_bboxes.dtype)], axis = 0)

        #     #_labels = tf.Print(_labels, [_labels], message='_labels:', summarize=1000)

        #     total_labels = tf.concat([gt_labels, _labels], axis = 0)
        #     total_scores = tf.concat([gt_scores, tf.ones_like(_labels, dtype=gt_scores.dtype)], axis = 0)

        #     def upsampel_impl(now_count, need_count):
        #         # sample with replacement
        #         left_count = need_count - now_count
        #         select_indices = tf.random_shuffle(tf.range(now_count))[:tf.floormod(left_count, now_count)]
        #         select_indices = tf.concat([tf.tile(tf.range(now_count), [tf.floor_div(left_count, now_count) + 1]), select_indices], axis = 0)

        #         return select_indices
        #     def downsample_impl(now_count, need_count):
        #         # downsample with replacement
        #         select_indices = tf.random_shuffle(tf.range(now_count))[:need_count]
        #         return select_indices
        #     #total_labels = tf.Print(total_labels, [total_labels], message='total_labels:', summarize=1000)

        #     #total_labels = tf.Print(total_labels, [tf.shape(total_labels), tf.shape(total_scores)], message='Notice Here: both the label and scores must be one vector.')

        #     positive_mask = total_labels > 0
        #     positive_indices = tf.squeeze(tf.where(positive_mask), axis = -1)
        #     n_positives = tf.shape(positive_indices)[0]

        #     #n_positives = tf.Print(n_positives, [n_positives], message='n_positives:', summarize=1000)

        #     # either downsample or take all
        #     fg_select_indices = tf.cond(n_positives < expected_num_fg_rois, lambda : positive_indices, lambda : tf.gather(positive_indices, downsample_impl(n_positives, expected_num_fg_rois)))
        #     # now the all rois taken as positive is min(n_positives, expected_num_fg_rois)

        #     negtive_mask = tf.logical_and(tf.logical_and(tf.logical_not(tf.logical_or(positive_mask, total_labels < 0)), total_scores < self._rpn_bg_high_thres), total_scores > self._rpn_bg_low_thres)
        #     negtive_indices = tf.squeeze(tf.where(negtive_mask), axis = -1)
        #     n_negtives = tf.shape(negtive_indices)[0]

        #     expected_num_bg_rois = rois_per_image - tf.minimum(n_positives, expected_num_fg_rois)
        #     # either downsample or take all
        #     bg_select_indices = tf.cond(n_negtives < expected_num_bg_rois, lambda : negtive_indices, lambda : tf.gather(negtive_indices, downsample_impl(n_negtives, expected_num_bg_rois)))
        #     # now the all rois taken as positive is min(n_negtives, expected_num_bg_rois)

        #     keep_indices = tf.concat([fg_select_indices, bg_select_indices], axis = 0)
        #     n_keeps = tf.shape(keep_indices)[0]
        #     # now n_keeps must be equal or less than rois_per_image
        #     final_keep_indices = tf.cond(n_keeps < rois_per_image, lambda : tf.gather(keep_indices, upsampel_impl(n_keeps, rois_per_image)), lambda : keep_indices)

        #     #print(tf.gather(total_rois, final_keep_indices), tf.gather(total_targets, final_keep_indices), tf.gather(total_labels, final_keep_indices), tf.gather(total_scores, final_keep_indices))
        #     return tf.gather(total_rois, final_keep_indices), tf.gather(total_targets, final_keep_indices), tf.gather(total_labels, final_keep_indices), tf.gather(total_scores, final_keep_indices)
            # return tf.gather(total_rois, final_keep_indices), tf.gather(total_targets, final_keep_indices), tf.gather(total_labels, final_keep_indices), tf.gather(total_scores, final_keep_indices)

        #print(tf.map_fn(lambda  _rois_labels_bboxes: encode_impl(_rois_labels_bboxes[0], _rois_labels_bboxes[1], _rois_labels_bboxes[2]), (all_rois, all_labels, all_bboxes), dtype=(tf.float32, tf.float32, tf.int64, tf.float32)))
        return tf.map_fn(lambda  _rois_labels_bboxes: encode_impl(_rois_labels_bboxes[0], _rois_labels_bboxes[1], _rois_labels_bboxes[2]), (all_rois, all_labels, all_bboxes), dtype=(tf.float32, tf.float32, tf.int64, tf.float32), back_prop=False)

    # return a list, of which each is:
    #   shape: [feature_h, feature_w, num_anchors, 4]
    #   order: ymin, xmin, ymax, xmax
    def decode_all_anchors(self, pred_location, squeeze_inner = False):
        assert len(self._anchors) == len(pred_location), 'predict location not equals to anchor priors.'
        pred_bboxes = []
        for index, location_ in enumerate(pred_location):
            # each location_:
            #   shape: [feature_h, feature_w, num_anchors, 4]
            #   order: cy, cx, h, w
            anchor = self._anchors[index]
            yref, xref, href, wref = tf.expand_dims(anchor[0], axis=-1), tf.expand_dims(anchor[1], axis=-1), anchor[2], anchor[3]
            # batch_size, feature_h, feature_w, num_anchors, 4
            location_ = tf.reshape(location_, [-1] + anchor[0].get_shape().as_list() + href.get_shape().as_list() + [4])

            inner_size = anchor[0].get_shape().as_list()[0] * anchor[0].get_shape().as_list()[1] * href.get_shape().as_list()[0]

            #print(yref.get_shape().as_list())
            def decode_impl(each_location):
                #each_location = tf.reshape(each_location, yref.get_shape().as_list()[:-1] + [4])
                pred_h = tf.exp(each_location[:, :, :, -2] * self._prior_scaling[2]) * href
                pred_w = tf.exp(each_location[:, :, :, -1] * self._prior_scaling[3]) * wref
                pred_cy = each_location[:, :, :, 0] * self._prior_scaling[0] * href + yref
                pred_cx = each_location[:, :, :, 1] * self._prior_scaling[1] * wref + xref
                return tf.stack(self.center2point(pred_cy, pred_cx, pred_h, pred_w), axis=-1)
            #location_ = tf.Print(location_,[location_])
            if squeeze_inner:
                pred_bboxes.append(tf.reshape(tf.map_fn(decode_impl, location_), [-1, inner_size, 4]))
            else:
                pred_bboxes.append(tf.map_fn(decode_impl, location_))

        return pred_bboxes

    def ext_decode_rois(self, proposals_roi, pred_location, head_prior_scaling=[1., 1., 1., 1.]):
        def ext_decode_impl(roi_pred):
            roi, pred = roi_pred[0], roi_pred[1]
            href, wref = (roi[:, 2] - roi[:, 0]), (roi[:, 3] - roi[:, 1])
            yref, xref = roi[:, 0] + href / 2., roi[:, 1] + wref / 2.,
            #each_location = tf.reshape(each_location, yref.get_shape().as_list()[:-1] + [4])
            pred_h = tf.exp(pred[:, -2] * head_prior_scaling[2]) * href
            pred_w = tf.exp(pred[:, -1] * head_prior_scaling[3]) * wref
            pred_cy = pred[:, 0] * head_prior_scaling[0] * href + yref
            pred_cx = pred[:, 1] * head_prior_scaling[1] * wref + xref
            return tf.stack([pred_cy - pred_h / 2., pred_cx - pred_w / 2., pred_cy + pred_h / 2., pred_cx + pred_w / 2.], axis=-1)

        return tf.map_fn(ext_decode_impl, (proposals_roi, pred_location), dtype=tf.float32, back_prop=False)


class AnchorCreator(object):
    def __init__(self, img_shape, layers_shapes, anchor_scales, extra_anchor_scales, anchor_ratios, layer_steps):
        super(AnchorCreator, self).__init__()
        # img_shape -> (height, width)
        self._img_shape = img_shape
        self._layers_shapes = layers_shapes
        self._anchor_scales = anchor_scales
        self._extra_anchor_scales = extra_anchor_scales
        self._anchor_ratios = anchor_ratios
        self._layer_steps = layer_steps
        self._anchor_offset = [0.5] * len(self._layers_shapes)

    def get_layer_anchors(self, layer_shape, anchor_scale, extra_anchor_scale, anchor_ratio, layer_step, offset = 0.5):
        ''' assume layer_shape[0] = 6, layer_shape[1] = 5
        x_on_layer = [[0, 1, 2, 3, 4],
                       [0, 1, 2, 3, 4],
                       [0, 1, 2, 3, 4],
                       [0, 1, 2, 3, 4],
                       [0, 1, 2, 3, 4],
                       [0, 1, 2, 3, 4]]
        y_on_layer = [[0, 0, 0, 0, 0],
                       [1, 1, 1, 1, 1],
                       [2, 2, 2, 2, 2],
                       [3, 3, 3, 3, 3],
                       [4, 4, 4, 4, 4],
                       [5, 5, 5, 5, 5]]
        '''
        x_on_layer, y_on_layer = tf.meshgrid(tf.range(layer_shape[1]), tf.range(layer_shape[0]))

        y_on_image = (tf.cast(y_on_layer, tf.float32) + offset) * layer_step / self._img_shape[0]
        x_on_image = (tf.cast(x_on_layer, tf.float32) + offset) * layer_step / self._img_shape[1]

        num_anchors = len(anchor_scale) * len(anchor_ratio) + len(extra_anchor_scale)

        #x_on_image = tf.Print(x_on_image, [x_on_layer], message='x_on_layer: ', summarize=1000)
        #y_on_image = tf.Print(y_on_image, [y_on_layer], message='y_on_layer: ', summarize=1000)

        list_h_on_image = []
        list_w_on_image = []

        global_index = 0
        for _, scale in enumerate(extra_anchor_scale):
            # h_on_image[global_index] = scale
            # w_on_image[global_index] = scale
            list_h_on_image.append(scale)
            list_w_on_image.append(scale)
            global_index += 1
        for scale_index, scale in enumerate(anchor_scale):
            for ratio_index, ratio in enumerate(anchor_ratio):
                # h_on_image[global_index] = scale  / math.sqrt(ratio)
                # w_on_image[global_index] = scale  * math.sqrt(ratio)
                list_h_on_image.append(scale / math.sqrt(ratio))
                list_w_on_image.append(scale * math.sqrt(ratio))
                global_index += 1
        # shape:
        # y_on_image, x_on_image: layers_shapes[0] * layers_shapes[1]
        # h_on_image, w_on_image: num_anchors
        return y_on_image, x_on_image, tf.constant(list_h_on_image, dtype=tf.float32), tf.constant(list_w_on_image, dtype=tf.float32), num_anchors

    def get_all_anchors(self):
        all_anchors = []
        num_anchors = []
        for layer_index, layer_shape in enumerate(self._layers_shapes):
            anchors_this_layer = self.get_layer_anchors(layer_shape,
                                                        self._anchor_scales[layer_index],
                                                        self._extra_anchor_scales[layer_index],
                                                        self._anchor_ratios[layer_index],
                                                        self._layer_steps[layer_index],
                                                        self._anchor_offset[layer_index])
            all_anchors.append(anchors_this_layer[:-1])
            num_anchors.append(anchors_this_layer[-1])
        return all_anchors, num_anchors

# procedure from Detectron of Facebook
# 1. for each location i in a (H, W) grid:
#      generate A anchor boxes centered on cell i
#      apply predicted bbox deltas to each of the A anchors at cell i
# 2. clip predicted boxes to image (may result in proposals with zero area that will be removed in the next step)
# 3. remove predicted boxes with either height or width < threshold
# 4. sort all (proposal, score) pairs by score from highest to lowest
# 5. take the top pre_nms_topN proposals before NMS (e.g. 6000)
# 6. apply NMS with a loose threshold (0.7) to the remaining proposals
# 7. take after_nms_topN (e.g. 300) proposals after NMS
# 8. return the top proposals
class BBoxUtils(object):
    @staticmethod
    def tf_bboxes_nms(scores, labels, bboxes, nms_threshold = 0.5, keep_top_k = 200, mode = 'min', scope=None):
        with tf.name_scope(scope, 'tf_bboxes_nms', [scores, labels, bboxes]):
            num_anchors = tf.shape(scores)[0]
            def nms_proc(scores, labels, bboxes):
                # sort all the bboxes
                scores, idxes = tf.nn.top_k(scores, k = num_anchors, sorted = True)
                labels, bboxes = tf.gather(labels, idxes), tf.gather(bboxes, idxes)

                ymin = bboxes[:, 0]
                xmin = bboxes[:, 1]
                ymax = bboxes[:, 2]
                xmax = bboxes[:, 3]

                vol_anchors = (xmax - xmin) * (ymax - ymin)

                nms_mask = tf.cast(tf.ones_like(scores, dtype=tf.int8), tf.bool)
                keep_mask = tf.cast(tf.zeros_like(scores, dtype=tf.int8), tf.bool)

                def safe_divide(numerator, denominator):
                    return tf.where(tf.greater(denominator, 0), tf.divide(numerator, denominator), tf.zeros_like(denominator))

                def get_scores(bbox, nms_mask):
                    # the inner square
                    inner_ymin = tf.maximum(ymin, bbox[0])
                    inner_xmin = tf.maximum(xmin, bbox[1])
                    inner_ymax = tf.minimum(ymax, bbox[2])
                    inner_xmax = tf.minimum(xmax, bbox[3])
                    h = tf.maximum(inner_ymax - inner_ymin, 0.)
                    w = tf.maximum(inner_xmax - inner_xmin, 0.)
                    inner_vol = h * w
                    this_vol = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                    if mode == 'union':
                        union_vol = vol_anchors - inner_vol  + this_vol
                    elif mode == 'min':
                        union_vol = tf.minimum(vol_anchors, this_vol)
                    else:
                        raise ValueError('unknown mode to use for nms.')
                    return safe_divide(inner_vol, union_vol) * tf.cast(nms_mask, tf.float32)

                def condition(index, nms_mask, keep_mask):
                    return tf.logical_and(tf.reduce_sum(tf.cast(nms_mask, tf.int32)) > 0, tf.less(index, keep_top_k))

                def body(index, nms_mask, keep_mask):
                    # at least one True in nms_mask
                    indices = tf.where(nms_mask)[0][0]
                    bbox = bboxes[indices]
                    this_mask = tf.one_hot(indices, num_anchors, on_value=False, off_value=True, dtype=tf.bool)
                    keep_mask = tf.logical_or(keep_mask, tf.logical_not(this_mask))
                    nms_mask = tf.logical_and(nms_mask, this_mask)

                    nms_scores = get_scores(bbox, nms_mask)

                    nms_mask = tf.logical_and(nms_mask, nms_scores < nms_threshold)
                    return [index+1, nms_mask, keep_mask]

                index = 0
                [index, nms_mask, keep_mask] = tf.while_loop(condition, body, [index, nms_mask, keep_mask])
                return tf.boolean_mask(scores, keep_mask), tf.boolean_mask(labels, keep_mask), tf.boolean_mask(bboxes, keep_mask)

            return tf.cond(tf.less(num_anchors, 1), lambda: (scores, labels, bboxes), lambda: nms_proc(scores, labels, bboxes))

    @staticmethod
    def tf_bboxes_nms_by_class(scores, labels, bboxes, num_classes, nms_threshold = 0.5, keep_top_k = 200, mode = 'min', scope=None):
        with tf.name_scope(scope, 'tf_bboxes_nms_by_class', [scores, labels, bboxes]):
            num_anchors = tf.shape(scores)[0]
            def nms_proc(scores, labels, bboxes):
                # sort all the bboxes
                scores, idxes = tf.nn.top_k(scores, k = num_anchors, sorted = True)
                labels, bboxes = tf.gather(labels, idxes), tf.gather(bboxes, idxes)

                ymin = bboxes[:, 0]
                xmin = bboxes[:, 1]
                ymax = bboxes[:, 2]
                xmax = bboxes[:, 3]

                vol_anchors = (xmax - xmin) * (ymax - ymin)

                total_keep_mask = tf.cast(tf.zeros_like(scores, dtype=tf.int8), tf.bool)

                def safe_divide(numerator, denominator):
                    return tf.where(tf.greater(denominator, 0), tf.divide(numerator, denominator), tf.zeros_like(denominator))

                def get_scores(bbox, nms_mask):
                    # the inner square
                    inner_ymin = tf.maximum(ymin, bbox[0])
                    inner_xmin = tf.maximum(xmin, bbox[1])
                    inner_ymax = tf.minimum(ymax, bbox[2])
                    inner_xmax = tf.minimum(xmax, bbox[3])
                    h = tf.maximum(inner_ymax - inner_ymin, 0.)
                    w = tf.maximum(inner_xmax - inner_xmin, 0.)
                    inner_vol = h * w
                    this_vol = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                    if mode == 'union':
                        union_vol = vol_anchors - inner_vol  + this_vol
                    elif mode == 'min':
                        union_vol = tf.minimum(vol_anchors, this_vol)
                    else:
                        raise ValueError('unknown mode to use for nms.')
                    return safe_divide(inner_vol, union_vol) * tf.cast(nms_mask, tf.float32)

                def condition(index, nms_mask, keep_mask):
                    return tf.logical_and(tf.reduce_sum(tf.cast(nms_mask, tf.int32)) > 0, tf.less(index, keep_top_k))

                def body(index, nms_mask, keep_mask):
                    # at least one True in nms_mask
                    indices = tf.where(nms_mask)[0][0]
                    bbox = bboxes[indices]
                    this_mask = tf.one_hot(indices, num_anchors, on_value=False, off_value=True, dtype=tf.bool)
                    keep_mask = tf.logical_or(keep_mask, tf.logical_not(this_mask))
                    nms_mask = tf.logical_and(nms_mask, this_mask)

                    nms_scores = get_scores(bbox, nms_mask)

                    nms_mask = tf.logical_and(nms_mask, nms_scores < nms_threshold)
                    return [index+1, nms_mask, keep_mask]
                def nms_loop_for_each(cls_index, total_keep_mask):
                    index = 0
                    nms_mask = tf.equal(tf.cast(cls_index, tf.int64), labels)
                    keep_mask = tf.cast(tf.zeros_like(scores, dtype=tf.int8), tf.bool)

                    [_, _, keep_mask] = tf.while_loop(condition, body, [index, nms_mask, keep_mask])
                    total_keep_mask = tf.logical_or(total_keep_mask, keep_mask)

                    return cls_index + 1, total_keep_mask
                cls_index = 1
                [_, total_keep_mask] = tf.while_loop(lambda cls_index, _: tf.less(cls_index, num_classes), nms_loop_for_each, [cls_index, total_keep_mask])
                indices_to_select = tf.where(total_keep_mask)
                select_mask = tf.cond(tf.less(tf.shape(indices_to_select)[0], keep_top_k + 1),
                                    lambda: total_keep_mask,
                                    lambda: tf.logical_and(total_keep_mask, tf.range(tf.cast(tf.shape(total_keep_mask)[0], tf.int64), dtype=tf.int64) < indices_to_select[keep_top_k][0]))
                return tf.boolean_mask(scores, select_mask), tf.boolean_mask(labels, select_mask), tf.boolean_mask(bboxes, select_mask)

            return tf.cond(tf.less(num_anchors, 1), lambda: (scores, labels, bboxes), lambda: nms_proc(scores, labels, bboxes))

    @staticmethod
    def filter_boxes(scores, labels, bboxes, min_size_ratio, image_shape, net_input_shape):
        """Only keep boxes with both sides >= min_size and center within the image.
        min_size_ratio is the ratio relative to net input shape
        """
        # Scale min_size to match image scale
        min_size = tf.maximum(0.0001, min_size_ratio * tf.sqrt(tf.cast(image_shape[0] * image_shape[1], tf.float32) / (net_input_shape[0] * net_input_shape[1])))

        ymin = bboxes[:, 0]
        xmin = bboxes[:, 1]

        ws = bboxes[:, 3] - xmin
        hs = bboxes[:, 2] - ymin

        x_ctr = xmin + ws / 2.
        y_ctr = ymin + hs / 2.

        keep_mask = tf.logical_and(tf.greater(ws, min_size), tf.greater(hs, min_size))
        keep_mask = tf.logical_and(keep_mask, tf.greater(x_ctr, 0.))
        keep_mask = tf.logical_and(keep_mask, tf.greater(y_ctr, 0.))
        keep_mask = tf.logical_and(keep_mask, tf.less(x_ctr, 1.))
        keep_mask = tf.logical_and(keep_mask, tf.less(y_ctr, 1.))

        return tf.boolean_mask(scores, keep_mask), tf.boolean_mask(labels, keep_mask), tf.boolean_mask(bboxes, keep_mask)
