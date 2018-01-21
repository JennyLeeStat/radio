# pylint: disable=too-many-statements
""" Contains Keras3DUNet model class. """

from functools import wraps
import tensorflow as tf
import keras
from keras.layers import (Input,
                          concatenate,
                          Conv3D,
                          MaxPooling3D,
                          UpSampling3D,
                          Activation)
from keras.layers.core import Activation
from keras.layers.normalization import BatchNormalization

from .keras_model import KerasModel
from .losses import dice_loss


class Keras3DUNet(KerasModel):
    """ Model incapsulating 3D U-Net architecture for 3D scans implemented in keras.

    Class extends KerasModel class.

    Contains description of 'bottleneck_block', 'reduction_block' and
    'upsampling_block'. Current 3D U-Net architecture is implemented
    inside _build method using these blocks.

    Architecture is inspired by 3D U-Net (Çiçek et Al., https://arxiv.org/abs/1606.06650).

    Notes
    -----
    Implementation requires the input tensor having shape=(batch_size, 1, 32, 64, 64).
    """
    def build_config(self):
        input_shape = self.get('input_shape', self.config, (1, 32, 64, 64))
        self.config.update({'input_shape': input_shape})
        super().build_config()

    def bottleneck_block(self, inputs, filters, scope, padding='same'):
        """ Apply bottleneck block transform to input tensor.

        Parameters
        ----------
        inputs : keras tensor
            input tensor.
        filters : int
            number of output filters required by Conv3D operation.
        scope : str
            scope name for block, will be used as an argument of tf.variable_scope.
        padding : str
            padding mode, can be 'same' or 'valid'.

        Returns
        -------
        keras tensor
            output tensor.

        Notes
        -----
        `channels_first` dim-ordering is used.
        """
        with tf.variable_scope(scope):
            conv1 = Conv3D(filters, (3, 3, 3),
                           data_format='channels_first',
                           padding=padding)(inputs)
            conv1 = BatchNormalization(axis=1, momentum=0.1,
                                       scale=True)(conv1)
            conv1 = Activation('relu')(conv1)

            conv2 = Conv3D(filters, (3, 3, 3),
                           data_format='channels_first',
                           padding=padding)(conv1)
            conv2 = BatchNormalization(axis=1, momentum=0.1,
                                       scale=True)(conv2)
            conv2 = Activation('relu')(conv2)
        return conv2

    def reduction_block(self, inputs, filters, scope, pool_size=(2, 2, 2), padding='same'):
        """ Apply reduction block transform to input tensor.

        Layer consists of two 3D-convolutional layers with batch normalization
        before 'relu' activation and max_pooling3d layer in the end.

        Parameters
        ----------
        inputs : keras tensor
            input tensor.
        filters : int
            number of filters in first and second covnolutions.
        scope : str
            scope name for block, will be used as an argument of tf.variable_scope.
        pool_size : tuple(int, int, int)
            size of pooling kernel along three axis, required by Conv3D operation.
        padding : str
            padding mode for convolutions, can be 'same' or 'valid'.

        Returns
        -------
        keras tensor
            output tensor.

        Notes
        -----
        `channels_first` dim-ordering is used.
        """
        with tf.variable_scope(scope):
            conv1 = Conv3D(filters, (3, 3, 3),
                           data_format='channels_first',
                           padding=padding)(inputs)
            conv1 = BatchNormalization(axis=1, momentum=0.1,
                                       scale=True)(conv1)
            conv1 = Activation('relu')(conv1)

            conv2 = Conv3D(filters, (3, 3, 3),
                           data_format='channels_first',
                           padding=padding)(conv1)
            conv2 = BatchNormalization(axis=1, momentum=0.1,
                                       scale=True)(conv2)
            conv2 = Activation('relu')(conv2)

            max_pool = MaxPooling3D(data_format='channels_first',
                                    pool_size=pool_size)(conv2)
        return conv2, max_pool

    def upsampling_block(self, inputs, skip_connect_tensor, filters, scope, padding='same'):
        """ Apply upsampling transform to two input tensors.

        First of all, UpSampling3D transform is applied to inputs. Then output
        tensor of operation is concatenated with skip_connect_tensor. After this
        two 3D-convolutions with batch normalization before 'relu' activation
        are applied.

        Parameters
        ----------
        inputs : keras tensor
            input tensor from previous layer.
        skip_connect_tensor : keras tensor
            input tensor from simmiliar layer from reduction branch of 3D U-Net.
        filters : int
            number of filters in convolutional layers.
        scope : str
            name of scope for block.
        padding : str
            padding mode for convolutions, can be 'same' or 'valid'.

        Returns
        -------
        keras tensor
            ouput tensor.

        Notes
        -----
        `channels_first` dim-ordering is used.
        """
        with tf.variable_scope(scope):
            upsample_tensor = UpSampling3D(data_format="channels_first",
                                           size=(2, 2, 2))(inputs)
            upsample_tensor = concatenate([upsample_tensor, skip_connect_tensor], axis=1)

            conv1 = Conv3D(filters, (3, 3, 3),
                           data_format="channels_first",
                           padding="same")(upsample_tensor)
            conv1 = BatchNormalization(axis=1, momentum=0.1,
                                       scale=True)(conv1)
            conv1 = Activation('relu')(conv1)

            conv2 = Conv3D(filters, (3, 3, 3),
                           data_format="channels_first",
                           padding="same")(conv1)
            conv2 = BatchNormalization(axis=1, momentum=0.1,
                                       scale=True)(conv2)
            conv2 = Activation('relu')(conv2)
        return conv2

    def _build(self, *args, **kwargs):
        """ Build 3D NoduleVnet model implemented in keras. """
        num_targets = self.get('num_targets', self.config)
        input_shape = self.get('input_shape', self.config)

        inputs = Input(shape=input_shape)

        # Downsampling or reduction layers: ReductionBlock_A, ReductionBlock_B, ReductionBlock_C, ReductionBlock_D
        # block_A has shape (None, 32, 64, 64, 32), reduct_block_A has shape (None, 16, 32, 32, 32)
        block_A, reduct_block_A = self.reduction_block(inputs, 32,
                                                       scope='ReductionBlock_A')

        # block_B has shape (None, 16, 32, 32, 64), reduct_block_B has shape (None, 8, 16, 16, 64)
        block_B, reduct_block_B = self.reduction_block(reduct_block_A, 64,
                                                       scope='ReductionBlock_B')

        # block_C has shape (None, 8, 16, 16, 128), reduct_block_C has shape (None, 4, 8, 8, 128)
        block_C, reduct_block_C = self.reduction_block(reduct_block_B, 128,
                                                       scope='ReductionBlock_C')

        # block_D has shape (None, 4, 8, 8, 256), reduct_block_D has shape (None, 2, 4, 4, 256)
        block_D, reduct_block_D = self.reduction_block(reduct_block_C, 256,
                                                       scope='ReductionBlock_D')

        # Bottleneck layer
        # bottleneck_block has shape (None, 2, 4, 4, 512)
        bottleneck_block = self.bottleneck_block(reduct_block_D, 512, 'BottleNeckBlock')

        # Upsampling Layers: UpsamplingBlock_D, UpsamplingBlock_C, UpsamplingBlock_B, UpsamplingBlock_A
        # upsample_block_C has shape (None, 4, 8, 8, 256)
        upsample_block_D = self.upsampling_block(bottleneck_block, block_D,
                                                 256, scope='UpsamplingBlock_D')

        # upsample_block_C has shape (None, 8, 16, 16, 128)
        upsample_block_C = self.upsampling_block(upsample_block_D, block_C,
                                                 128, scope='UpsamplingBlock_C')

        # upsample_block_B has shape (None, 16, 32, 32, 64)
        upsample_block_B = self.upsampling_block(upsample_block_C, block_B,
                                                 64, scope='UpsamplingBlock_B')

        # upsample_block_A has shape (None, 32, 64, 64, 32)
        upsample_block_A = self.upsampling_block(upsample_block_B, block_A,
                                                 32, scope='UpsamplingBlock_A')

        # Final convolution
        final_conv = Conv3D(num_targets, (1, 1, 1),
                            activation='sigmoid',
                            data_format="channels_first",
                            padding='same')(upsample_block_A)

        return [inputs], [final_conv]

    @wraps(keras.models.Model.compile)
    def compile(self, optimizer='adam', loss=dice_loss, **kwargs):
        """ Compile 3D U-Net model. """
        super().compile(optimizer=optimizer, loss=loss)
