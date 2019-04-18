import tensorflow as tf
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import to_rgb
from tensorflow.python.ops.rnn import dynamic_rnn
import sonnet as snt

from dps import cfg
from dps.utils import Param, animate
from dps.utils.tf import (
    build_scheduled_value, FIXED_COLLECTION, tf_mean_sum, MLP,
    RenderHook, tf_shape, ConvNet, RecurrentGridConvNet, tf_roll
)

from auto_yolo.models.core import normal_vae, TensorRecorder, xent_loss


class VideoNetwork(TensorRecorder):
    attr_prior_mean = Param()
    attr_prior_std = Param()
    noisy = Param()

    needs_background = True
    background_encoder = None
    background_decoder = None

    eval_funcs = dict()

    def __init__(self, env, updater, scope=None, **kwargs):
        self.updater = updater

        self.obs_shape = env.datasets['train'].obs_shape
        self.n_frames, self.image_height, self.image_width, self.image_depth = self.obs_shape

        super(VideoNetwork, self).__init__(scope=scope, **kwargs)

    def std_nonlinearity(self, std_logit):
        # return tf.exp(std)
        std = 2 * tf.nn.sigmoid(tf.clip_by_value(std_logit, -10, 10))
        if not self.noisy:
            std = tf.zeros_like(std)
        return std

    @property
    def inp(self):
        return self._tensors["inp"]

    @property
    def batch_size(self):
        return self._tensors["batch_size"]

    @property
    def is_training(self):
        return self._tensors["is_training"]

    @property
    def float_is_training(self):
        return self._tensors["float_is_training"]

    def _call(self, data, is_training):
        inp = data["image"]

        self._tensors = dict(
            inp=inp,
            is_training=is_training,
            float_is_training=tf.to_float(is_training),
            batch_size=tf.shape(inp)[0],
        )

        if "annotations" in data:
            self._tensors.update(
                annotations=data["annotations"]["data"],
                n_annotations=data["annotations"]["shapes"][:, 1],
                n_valid_annotations=tf.to_int32(
                    tf.reduce_sum(
                        data["annotations"]["data"][:, :, :, 0]
                        * tf.to_float(data["annotations"]["mask"][:, :, :, 0]),
                        axis=2
                    )
                )
            )

        if "label" in data:
            self._tensors.update(
                targets=data["label"],
            )

        if "background" in data:
            self._tensors.update(
                background=data["background"],
            )

        self.record_tensors(
            batch_size=tf.to_float(self.batch_size),
            float_is_training=self.float_is_training
        )

        self.losses = dict()

        with tf.variable_scope("representation", reuse=self.initialized):
            if self.needs_background:
                self.build_background()
            self.build_representation()

        return dict(
            tensors=self._tensors,
            recorded_tensors=self.recorded_tensors,
            losses=self.losses,
        )

    def build_background(self):
        if self.needs_background:

            if cfg.background_cfg.mode == "colour":
                rgb = np.array(to_rgb(cfg.background_cfg.colour))[None, None, None, :]
                background = rgb * tf.ones_like(self.inp)

            elif cfg.background_cfg.mode == "learn_solid":
                # Learn a solid colour for the background
                self.solid_background_logits = tf.get_variable("solid_background", initializer=[0.0, 0.0, 0.0])
                if "background" in self.fixed_weights:
                    tf.add_to_collection(FIXED_COLLECTION, self.solid_background_logits)
                solid_background = tf.nn.sigmoid(10 * self.solid_background_logits)
                background = solid_background[None, None, None, :] * tf.ones_like(self.inp)

            elif cfg.background_cfg.mode == "learn_and_transform":
                if self.background_encoder is None:
                    self.background_encoder = cfg.background_cfg.build_encoder(scope="background_encoder")
                    if "background_encoder" in self.fixed_weights:
                        self.background_encoder.fix_variables()

                if self.background_decoder is None:
                    self.background_decoder = cfg.background_cfg.build_decoder(scope="background_decoder")
                    if "background_decoder" in self.fixed_weights:
                        self.background_decoder.fix_variables()

                # --- encode ---

                n_transform_latents = 4
                n_latents = (2 * cfg.background_cfg.A, 2 * n_transform_latents)

                bg_attr, bg_transform_params = self.background_encoder(self.inp, n_latents, self.is_training)

                # --- bg attributes ---

                bg_attr_mean, bg_attr_log_std = tf.split(bg_attr, 2, axis=-1)
                bg_attr_std = self.std_nonlinearity(bg_attr_log_std)

                bg_attr, bg_attr_kl = normal_vae(bg_attr_mean, bg_attr_std, self.attr_prior_mean, self.attr_prior_std)

                # --- bg location ---

                bg_transform_params = tf.reshape(
                    bg_transform_params,
                    (self.batch_size, self.n_frames, 2*n_transform_latents))

                mean, log_std = tf.split(bg_transform_params, 2, axis=2)
                std = self.std_nonlinearity(log_std)

                logits, kl = normal_vae(mean, std, 0.0, 1.0)

                # integrate across timesteps
                logits = tf.cumsum(logits, axis=1)
                logits = tf.reshape(logits, (self.batch_size*self.n_frames, n_transform_latents))

                y, x, h, w = tf.split(logits, n_transform_latents, axis=1)
                h = (0.9 - 0.5) * tf.nn.sigmoid(h) + 0.5
                w = (0.9 - 0.5) * tf.nn.sigmoid(w) + 0.5
                y = (1 - h) * tf.nn.tanh(y)
                x = (1 - w) * tf.nn.tanh(x)

                # --- decode ---

                background = self.background_decoder(bg_attr, self.image_depth, self.is_training)
                bg_shape = cfg.background_cfg.bg_shape
                background = background[:, :bg_shape[0], :bg_shape[1], :]
                assert background.shape[1:3] == bg_shape
                background_raw = tf.nn.sigmoid(tf.clip_by_value(background, -10, 10))

                transform_constraints = snt.AffineWarpConstraints.no_shear_2d()

                warper = snt.AffineGridWarper(
                    bg_shape, (self.image_height, self.image_width), transform_constraints)

                transforms = tf.concat([w, x, h, y], axis=-1)
                grid_coords = warper(transforms)

                grid_coords = tf.reshape(grid_coords, (self.batch_size, self.n_frames, *tf_shape(grid_coords)[1:]))

                background = tf.contrib.resampler.resampler(background_raw, grid_coords)

                self._tensors.update(
                    bg_attr_mean=bg_attr_mean,
                    bg_attr_std=bg_attr_std,
                    bg_attr_kl=bg_attr_kl,
                    bg_attr=bg_attr,
                    bg_y=tf.reshape(y, (self.batch_size, self.n_frames, 1)),
                    bg_x=tf.reshape(x, (self.batch_size, self.n_frames, 1)),
                    bg_h=tf.reshape(h, (self.batch_size, self.n_frames, 1)),
                    bg_w=tf.reshape(w, (self.batch_size, self.n_frames, 1)),
                    bg_transform_kl=kl,
                    bg_raw=background_raw,
                )

            elif cfg.background_cfg.mode == "data":
                background = self._tensors["background"]
            else:
                raise Exception("Unrecognized background mode: {}.".format(cfg.background_cfg.mode))

            self._tensors["background"] = background


class BackgroundExtractor(RecurrentGridConvNet):
    bidirectional = True

    bg_head = None
    transform_head = None

    def _call(self, inp, output_size, is_training):
        if self.bg_head is None:
            self.bg_head = ConvNet(
                layers=[
                    dict(filters=None, kernel_size=1, strides=1, padding="SAME"),
                    dict(filters=None, kernel_size=1, strides=1, padding="SAME"),
                ],
                scope="bg_head"
            )

        if self.transform_head is None:
            self.transform_head = MLP(n_units=[64, 64], scope="transform_head")

        n_attr_channels, n_transform_values = output_size
        processed, n_grid_cells, grid_cell_size = super()._call(inp, n_attr_channels, is_training)
        B, F, H, W, C = tf_shape(processed)

        # Map processed to shapes (B, H, W, C) and (B, F, 2)

        bg_attrs = self.bg_head(tf.reduce_mean(processed, axis=1), None, is_training)

        transform_values = self.transform_head(
            tf.reshape(processed, (B*F, H*W*C)),
            n_transform_values, is_training)

        transform_values = tf.reshape(transform_values, (B, F, n_transform_values))

        return bg_attrs, transform_values


class SimpleVideoVAE(VideoNetwork):
    """ Encode each frame with an encoder, use a recurrent network to link between latent
        representations of different frames (in a causal direction), apply a decoder to the
        frame-wise latest representations to come up with reconstructions of each frame.

    """
    attr_prior_mean = Param()
    attr_prior_std = Param()

    A = Param()

    train_reconstruction = Param()
    reconstruction_weight = Param()

    train_kl = Param()
    kl_weight = Param()
    noisy = Param()

    build_encoder = Param()
    build_decoder = Param()
    build_cell = Param()

    encoder = None
    decoder = None
    cell = None
    needs_background = False

    def __init__(self, env, updater, scope=None, **kwargs):
        super().__init__(env, updater, scope=scope)

        self.attr_prior_mean = build_scheduled_value(self.attr_prior_mean, "attr_prior_mean")
        self.attr_prior_std = build_scheduled_value(self.attr_prior_std, "attr_prior_std")

        self.reconstruction_weight = build_scheduled_value(
            self.reconstruction_weight, "reconstruction_weight")
        self.kl_weight = build_scheduled_value(self.kl_weight, "kl_weight")

        if not self.noisy and self.train_kl:
            raise Exception("If `noisy` is False, `train_kl` must also be False.")

    def build_representation(self):
        # --- init modules ---

        if self.encoder is None:
            self.encoder = self.build_encoder(scope="encoder")
            if "encoder" in self.fixed_weights:
                self.encoder.fix_variables()

        if self.cell is None:
            self.cell = cfg.build_cell(scope="cell")
            if "cell" in self.fixed_weights:
                self.cell.fix_variables()

        if self.decoder is None:
            self.decoder = cfg.build_decoder(scope="decoder")
            if "decoder" in self.fixed_weights:
                self.decoder.fix_variables()

        # --- encode ---

        video = tf.reshape(self.inp, (self.batch_size * self.n_frames, *self.obs_shape[1:]))
        encoder_output = self.encoder(video, 2 * self.A, self.is_training)
        encoder_output = tf.layers.flatten(encoder_output)
        encoder_output = tf.reshape(encoder_output, (self.batch_size, self.n_frames, encoder_output.shape[1]))

        attr, final_state = dynamic_rnn(
            self.cell, encoder_output, initial_state=self.cell.zero_state(self.batch_size, tf.float32),
            parallel_iterations=1, swap_memory=False, time_major=False)

        attr_mean, attr_log_std = tf.split(attr, 2, axis=-1)
        attr_std = tf.math.softplus(attr_log_std)

        if not self.noisy:
            attr_std = tf.zeros_like(attr_std)

        attr, attr_kl = normal_vae(attr_mean, attr_std, self.attr_prior_mean, self.attr_prior_std)

        self._tensors.update(attr_mean=attr_mean, attr_std=attr_std, attr_kl=attr_kl, attr=attr)

        # --- decode ---

        decoder_input = tf.reshape(attr, (self.batch_size*self.n_frames, attr.shape[2]))

        reconstruction = self.decoder(decoder_input, self.inp.shape[2:], self.is_training)
        reconstruction = reconstruction[:, :self.obs_shape[1], :self.obs_shape[2], :]
        reconstruction = tf.reshape(reconstruction, (self.batch_size, *self.obs_shape))

        reconstruction = tf.nn.sigmoid(tf.clip_by_value(reconstruction, -10, 10))
        self._tensors["output"] = reconstruction

        # --- losses ---

        if self.train_kl:
            self.losses['attr_kl'] = tf_mean_sum(self._tensors["attr_kl"])

        if self.train_reconstruction:
            self._tensors['per_pixel_reconstruction_loss'] = xent_loss(pred=reconstruction, label=self.inp)
            self.losses['reconstruction'] = tf_mean_sum(self._tensors['per_pixel_reconstruction_loss'])


class SimpleVAE_RenderHook(RenderHook):
    def __call__(self, updater):
        self.fetches = "inp output"

        if 'prediction' in updater.network._tensors:
            self.fetches += " prediction targets"

        fetched = self._fetch(updater)
        self._plot_reconstruction(updater, fetched)

    @staticmethod
    def normalize_images(images):
        mx = images.reshape(*images.shape[:-3], -1).max(axis=-1)
        return images / mx[..., None, None, None]

    def _plot_reconstruction(self, updater, fetched):
        inp = fetched['inp']
        output = fetched['output']

        fig_height = 20
        fig_width = 4.5 * fig_height

        diff = self.normalize_images(np.abs(inp - output).sum(axis=-1, keepdims=True) / output.shape[-1])
        xent = self.normalize_images(xent_loss(pred=output, label=inp, tf=False).sum(axis=-1, keepdims=True))

        path = self.path_for("animation", updater, "gif")
        fig, axes, anim = animate(
            inp, output, diff.astype('f'), xent.astype('f'),
            figsize=(fig_width, fig_height), path=path)
        plt.close()

        # prediction = fetched.get("prediction", None)
        # targets = fetched.get("targets", None)
        # if targets is not None:
        #     _target = targets[n]
        #     _prediction = prediction[n]

        #     title = "target={}, prediction={}".format(np.argmax(_target), np.argmax(_prediction))
        #     ax.set_title(title)

        # self.savefig("sampled_reconstruction", fig, updater)
