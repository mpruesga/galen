"""

This function is for training a network using synthetic scans generated from a set of training label maps.
See details in the docstring below.

If you use this code, please cite one of the SynthSeg papers:
https://github.com/BBillot/SynthSeg/blob/master/bibtex.bib

Copyright 2020 Benjamin Billot

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
compliance with the License. You may obtain a copy of the License at
https://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software distributed under the License is
distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
implied. See the License for the specific language governing permissions and limitations under the
License.
"""


# python imports
import os
import keras
import numpy as np
import tensorflow as tf
from keras import models
import keras.callbacks as KC
from keras.optimizers import Adam
from inspect import getmembers, isclass

# project imports
from Models.SynthSeg.SynthSeg import metrics_model as metrics
from Models.SynthSeg.SynthSeg.brain_generator import BrainGenerator

# third-party imports
from Models.SynthSeg.ext.lab2im import utils, layers
from Models.SynthSeg.ext.neuron import layers as nrn_layers
from Models.SynthSeg.ext.neuron import models as nrn_models


def training(labels_dir,
             model_dir,
             generation_labels=None,
             n_neutral_labels=None,
             segmentation_labels=None,
             subjects_prob=None,
             batchsize=1,
             n_channels=1,
             target_res=None,
             output_shape=None,
             generation_classes=None,
             prior_distributions='uniform',
             prior_means=None,
             prior_stds=None,
             use_specific_stats_for_channel=False,
             mix_prior_and_random=False,
             flipping=True,
             scaling_bounds=.2,
             rotation_bounds=15,
             shearing_bounds=.012,
             translation_bounds=False,
             nonlin_std=4.,
             nonlin_scale=.04,
             randomise_res=True,
             max_res_iso=4.,
             max_res_aniso=8.,
             data_res=None,
             thickness=None,
             bias_field_std=.7,
             bias_scale=.025,
             return_gradients=False,
             n_levels=5,
             nb_conv_per_level=2,
             conv_size=3,
             unet_feat_count=24,
             feat_multiplier=2,
             activation='elu',
             lr=1e-4,
             wl2_epochs=1,
             dice_epochs=50,
             steps_per_epoch=10000,
             checkpoint=None):
    """
    This function trains a UNet to segment MRI images with synthetic scans generated by sampling a GMM conditioned on
    label maps. We regroup the parameters in three categories: Generation, Architecture, Training.

    # IMPORTANT !!!
    # Each time we provide a parameter with separate values for each axis (e.g. with a numpy array or a sequence),
    # these values refer to the RAS axes.

    :param labels_dir: path of folder with all input label maps, or to a single label map (if only one training example)
    :param model_dir: path of a directory where the models will be saved during training.

    #---------------------------------------------- Generation parameters ----------------------------------------------
    # label maps parameters
    :param generation_labels: (optional) list of all possible label values in the input label maps.
    It can be None (default, where the label values are directly gotten from the provided label maps), a list,
    a 1d numpy array, or the path to such an array. If not None, the background label should always be the first. Then,
    if the label maps contain some right/left specific labels and if flipping is applied during training (see 'flipping'
    parameter), generation_labels should be organised as follows:
    first the background label, then the non-sided labels (i.e. those which are not right/left specific), then all the
    left labels, and finally the corresponding right labels (in the same order as the left ones). Please make sure each
    that each sided label has a right and a left value (this is essential!!!).
    :param n_neutral_labels: (optional) if the label maps contain some right/left specific labels and if flipping is
    applied during training, please provide the number of non-sided labels (including the background).
    This is used to know where the sided labels start in generation_labels. Leave to default (None) if either one of the
    two conditions is not fulfilled.
    :param segmentation_labels: (optional) list of the same length as generation_labels to indicate which values to use
    in the training label maps, i.e. all occurrences of generation_labels[i] in the input label maps will be converted
    to output_labels[i] in the returned label maps. Examples:
    Set output_labels[i] to zero if you wish to erase the value generation_labels[i] from the returned label maps.
    Set output_labels[i]=generation_labels[i] if you wish to keep the value generation_labels[i] in the returned maps.
    Can be a list or a 1d numpy array, or the path to such an array. Default is output_labels = generation_labels.
    :param subjects_prob: (optional) relative order of importance (doesn't have to be probabilistic), with which to pick
    the provided label maps at each minibatch. Can be a sequence, a 1D numpy array, or the path to such an array, and it
    must be as long as path_label_maps. By default, all label maps are chosen with the same importance.

    # output-related parameters
    :param batchsize: (optional) number of images to generate per mini-batch. Default is 1.
    :param n_channels: (optional) number of channels to be synthesised. Default is 1.
    :param target_res: (optional) target resolution of the generated images and corresponding label maps.
    If None, the outputs will have the same resolution as the input label maps.
    Can be a number (isotropic resolution), or the path to a 1d numpy array.
    :param output_shape: (optional) desired shape of the output image, obtained by randomly cropping the generated image
    Can be an integer (same size in all dimensions), a sequence, a 1d numpy array, or the path to a 1d numpy array.
    Default is None, where no cropping is performed.

    # GMM-sampling parameters
    :param generation_classes: (optional) Indices regrouping generation labels into classes of same intensity
    distribution. Regrouped labels will thus share the same Gaussian when sampling a new image. Should be the path to a
    1d numpy array with the same length as generation_labels. and contain values between 0 and K-1, where K is the total
    number of classes. Default is all labels have different classes.
    Can be a list or a 1d numpy array, or the path to such an array.
    :param prior_distributions: (optional) type of distribution from which we sample the GMM parameters.
    Can either be 'uniform', or 'normal'. Default is 'uniform'.
    :param prior_means: (optional) hyperparameters controlling the prior distributions of the GMM means. Because
    these prior distributions are uniform or normal, they require by 2 hyperparameters. Can be a path to:
    1) an array of shape (2, K), where K is the number of classes (K=len(generation_labels) if generation_classes is
    not given). The mean of the Gaussian distribution associated to class k in [0, ...K-1] is sampled at each mini-batch
    from U(prior_means[0,k], prior_means[1,k]) if prior_distributions is uniform, and from
    N(prior_means[0,k], prior_means[1,k]) if prior_distributions is normal.
    2) an array of shape (2*n_mod, K), where each block of two rows is associated to hyperparameters derived
    from different modalities. In this case, if use_specific_stats_for_channel is False, we first randomly select a
    modality from the n_mod possibilities, and we sample the GMM means like in 2).
    If use_specific_stats_for_channel is True, each block of two rows correspond to a different channel
    (n_mod=n_channels), thus we select the corresponding block to each channel rather than randomly drawing it.
    Default is None, which corresponds all GMM means sampled from uniform distribution U(25, 225).
    :param prior_stds: (optional) same as prior_means but for the standard deviations of the GMM.
    Default is None, which corresponds to U(5, 25).
    :param use_specific_stats_for_channel: (optional) whether the i-th block of two rows in the prior arrays must be
    only used to generate the i-th channel. If True, n_mod should be equal to n_channels. Default is False.
    :param mix_prior_and_random: (optional) if prior_means is not None, enables to reset the priors to their default
    values for half of these cases, and thus generate images of random contrast.

    # spatial deformation parameters
    :param flipping: (optional) whether to introduce right/left random flipping. Default is True.
    :param scaling_bounds: (optional) if apply_linear_trans is True, the scaling factor for each dimension is
    sampled from a uniform distribution of predefined bounds. Can either be:
    1) a number, in which case the scaling factor is independently sampled from the uniform distribution of bounds
    (1-scaling_bounds, 1+scaling_bounds) for each dimension.
    2) the path to a numpy array of shape (2, n_dims), in which case the scaling factor in dimension i is sampled from
    the uniform distribution of bounds (scaling_bounds[0, i], scaling_bounds[1, i]) for the i-th dimension.
    3) False, in which case scaling is completely turned off.
    Default is scaling_bounds = 0.2 (case 1)
    :param rotation_bounds: (optional) same as scaling bounds but for the rotation angle, except that for case 1 the
    bounds are centred on 0 rather than 1, i.e. (0+rotation_bounds[i], 0-rotation_bounds[i]).
    Default is rotation_bounds = 15.
    :param shearing_bounds: (optional) same as scaling bounds. Default is shearing_bounds = 0.012.
    :param translation_bounds: (optional) same as scaling bounds. Default is translation_bounds = False, but we
    encourage using it when cropping is deactivated (i.e. when output_shape=None).
    :param nonlin_std: (optional) Standard deviation of the normal distribution from which we sample the first
    tensor for synthesising the deformation field. Set to 0 to completely deactivate elastic deformation.
    :param nonlin_scale: (optional) Ratio between the size of the input label maps and the size of the sampled
    tensor for synthesising the elastic deformation field.

    # blurring/resampling parameters
    :param randomise_res: (optional) whether to mimic images that would have been 1) acquired at low resolution, and
    2) resampled to high resolution. The low resolution is uniformly resampled at each minibatch from [1mm, 9mm].
    In that process, the images generated by sampling the GMM are: 1) blurred at the sampled LR, 2) downsampled at LR,
    and 3) resampled at target_resolution.
    :param max_res_iso: (optional) If randomise_res is True, this enables to control the upper bound of the uniform
    distribution from which we sample the random resolution U(min_res, max_res_iso), where min_res is the resolution of
    the input label maps. Must be a number, and default is 4. Set to None to deactivate it, but if randomise_res is
    True, at least one of max_res_iso or max_res_aniso must be given.
    :param max_res_aniso: If randomise_res is True, this enables to downsample the input volumes to a random LR in
    only 1 (random) direction. This is done by randomly selecting a direction i in the range [0, n_dims-1], and sampling
    a value in the corresponding uniform distribution U(min_res[i], max_res_aniso[i]), where min_res is the resolution
    of the input label maps. Can be a number, a sequence, or a 1d numpy array. Set to None to deactivate it, but if
    randomise_res is True, at least one of max_res_iso or max_res_aniso must be given.
    :param data_res: (optional) specific acquisition resolution to mimic, as opposed to random resolution sampled when
    randomise_res is True. This triggers a blurring which mimics the acquisition resolution, but downsampling is
    optional (see param downsample). Default for data_res is None, where images are slightly blurred. If the generated
    images are uni-modal, data_res can be a number (isotropic acquisition resolution), a sequence, a 1d numpy array, or
    the path to a 1d numpy array. In the multi-modal case, it should be given as a numpy array (or a path) of size
    (n_mod, n_dims), where each row is the acquisition resolution of the corresponding channel.
    :param thickness: (optional) if data_res is provided, we can further specify the slice thickness of the low
    resolution images to mimic. Must be provided in the same format as data_res. Default thickness = data_res.

    # bias field parameters
    :param bias_field_std: (optional) If strictly positive, this triggers the corruption of images with a bias field.
    The bias field is obtained by sampling a first small tensor from a normal distribution, resizing it to
    full size, and rescaling it to positive values by taking the voxel-wise exponential. bias_field_std designates the
    std dev of the normal distribution from which we sample the first tensor.
    Set to 0 to completely deactivate bias field corruption.
    :param bias_scale: (optional) If bias_field_std is not False, this designates the ratio between the size of
    the input label maps and the size of the first sampled tensor for synthesising the bias field.

    :param return_gradients: (optional) whether to return the synthetic image or the magnitude of its spatial gradient
    (computed with Sobel kernels).

    # ------------------------------------------ UNet architecture parameters ------------------------------------------
    :param n_levels: (optional) number of level for the Unet. Default is 5.
    :param nb_conv_per_level: (optional) number of convolutional layers per level. Default is 2.
    :param conv_size: (optional) size of the convolution kernels. Default is 2.
    :param unet_feat_count: (optional) number of feature for the first layer of the UNet. Default is 24.
    :param feat_multiplier: (optional) multiply the number of feature by this number at each new level. Default is 2.
    :param activation: (optional) activation function. Can be 'elu', 'relu'.

    # ----------------------------------------------- Training parameters ----------------------------------------------
    :param lr: (optional) learning rate for the training. Default is 1e-4
    :param wl2_epochs: (optional) number of epochs for which the network (except the soft-max layer) is trained with L2
    norm loss function. Default is 1.
    :param dice_epochs: (optional) number of epochs with the soft Dice loss function. Default is 50.
    :param steps_per_epoch: (optional) number of steps per epoch. Default is 10000. Since no online validation is
    possible, this is equivalent to the frequency at which the models are saved.
    :param checkpoint: (optional) path of an already saved model to load before starting the training.
    """

    # check epochs
    assert (wl2_epochs > 0) | (dice_epochs > 0), \
        'either wl2_epochs or dice_epochs must be positive, had {0} and {1}'.format(wl2_epochs, dice_epochs)

    # get label lists
    generation_labels, _ = utils.get_list_labels(label_list=generation_labels, labels_dir=labels_dir)
    if segmentation_labels is not None:
        segmentation_labels, _ = utils.get_list_labels(label_list=segmentation_labels)
    else:
        segmentation_labels = generation_labels
    n_segmentation_labels = len(np.unique(segmentation_labels))

    # instantiate BrainGenerator object
    brain_generator = BrainGenerator(labels_dir=labels_dir,
                                     generation_labels=generation_labels,
                                     n_neutral_labels=n_neutral_labels,
                                     output_labels=segmentation_labels,
                                     subjects_prob=subjects_prob,
                                     batchsize=batchsize,
                                     n_channels=n_channels,
                                     target_res=target_res,
                                     output_shape=output_shape,
                                     output_div_by_n=2 ** n_levels,
                                     generation_classes=generation_classes,
                                     prior_distributions=prior_distributions,
                                     prior_means=prior_means,
                                     prior_stds=prior_stds,
                                     use_specific_stats_for_channel=use_specific_stats_for_channel,
                                     mix_prior_and_random=mix_prior_and_random,
                                     flipping=flipping,
                                     scaling_bounds=scaling_bounds,
                                     rotation_bounds=rotation_bounds,
                                     shearing_bounds=shearing_bounds,
                                     translation_bounds=translation_bounds,
                                     nonlin_std=nonlin_std,
                                     nonlin_scale=nonlin_scale,
                                     randomise_res=randomise_res,
                                     max_res_iso=max_res_iso,
                                     max_res_aniso=max_res_aniso,
                                     data_res=data_res,
                                     thickness=thickness,
                                     bias_field_std=bias_field_std,
                                     bias_scale=bias_scale,
                                     return_gradients=return_gradients)

    # generation model
    labels_to_image_model = brain_generator.labels_to_image_model
    unet_input_shape = brain_generator.model_output_shape

    # prepare the segmentation model
    unet_model = nrn_models.unet(input_model=labels_to_image_model,
                                 input_shape=unet_input_shape,
                                 nb_labels=n_segmentation_labels,
                                 nb_levels=n_levels,
                                 nb_conv_per_level=nb_conv_per_level,
                                 conv_size=conv_size,
                                 nb_features=unet_feat_count,
                                 feat_mult=feat_multiplier,
                                 activation=activation,
                                 batch_norm=-1,
                                 name='unet')

    # input generator
    input_generator = utils.build_training_generator(brain_generator.model_inputs_generator, batchsize)

    # pre-training with weighted L2, input is fit to the softmax rather than the probabilities
    if wl2_epochs > 0:
        wl2_model = models.Model(unet_model.inputs, [unet_model.get_layer('unet_likelihood').output])
        wl2_model = metrics.metrics_model(wl2_model, segmentation_labels, 'wl2')
        train_model(wl2_model, input_generator, lr, wl2_epochs, steps_per_epoch, model_dir, 'wl2', checkpoint)
        checkpoint = os.path.join(model_dir, 'wl2_%03d.h5' % wl2_epochs)

    # fine-tuning with dice metric
    dice_model = metrics.metrics_model(unet_model, segmentation_labels, 'dice')
    train_model(dice_model, input_generator, lr, dice_epochs, steps_per_epoch, model_dir, 'dice', checkpoint)


def train_model(model,
                generator,
                learning_rate,
                n_epochs,
                n_steps,
                model_dir,
                metric_type,
                path_checkpoint=None,
                reinitialise_momentum=False):

    # prepare model and log folders
    utils.mkdir(model_dir)
    log_dir = os.path.join(model_dir, 'logs')
    utils.mkdir(log_dir)

    # model saving callback
    save_file_name = os.path.join(model_dir, '%s_{epoch:03d}.h5' % metric_type)
    callbacks = [KC.ModelCheckpoint(save_file_name, verbose=1)]

    # TensorBoard callback
    if metric_type == 'dice':
        callbacks.append(KC.TensorBoard(log_dir=log_dir, histogram_freq=0, write_graph=True, write_images=False))

    compile_model = True
    init_epoch = 0
    if path_checkpoint is not None:
        if metric_type in path_checkpoint:
            init_epoch = int(os.path.basename(path_checkpoint).split(metric_type)[1][1:-3])
        if (not reinitialise_momentum) & (metric_type in path_checkpoint):
            custom_l2i = {key: value for (key, value) in getmembers(layers, isclass) if key != 'Layer'}
            custom_nrn = {key: value for (key, value) in getmembers(nrn_layers, isclass) if key != 'Layer'}
            custom_objects = {**custom_l2i, **custom_nrn, 'tf': tf, 'keras': keras, 'loss': metrics.IdentityLoss().loss}
            model = models.load_model(path_checkpoint, custom_objects=custom_objects)
            compile_model = False
        else:
            model.load_weights(path_checkpoint, by_name=True)

    # compile
    if compile_model:
        model.compile(optimizer=Adam(lr=learning_rate), loss=metrics.IdentityLoss().loss)

    # fit
    model.fit_generator(generator,
                        epochs=n_epochs,
                        steps_per_epoch=n_steps,
                        callbacks=callbacks,
                        initial_epoch=init_epoch)
