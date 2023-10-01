import numpy as np
from tqdm.auto import tqdm
from PIL import Image
import keras_cv_attention_models
from keras_cv_attention_models import backend
from keras_cv_attention_models.backend import layers, functional, models, initializers, image_data_format
from keras_cv_attention_models.clip import tokenizer
from keras_cv_attention_models.models import register_model, FakeModelWrapper  # FakeModelWrapper providing save / load / cuda class methods


@register_model
class StableDiffusion(FakeModelWrapper):
    def __init__(
        self,
        clip_model="beit.ViTTextLargePatch14",
        unet_model="stable_diffusion.UNet",
        decoder_model="stable_diffusion.Decoder",
        encoder_model=None,  # or "stable_diffusion.Encoder"
        clip_model_kwargs={},
        unet_model_kwargs={},
        decoder_model_kwargs={},
        encoder_model_kwargs={},
        caption_tokenizer="SimpleTokenizer",
        pretrained="default",  # default for using model preset weights, set None to disable all pretrained, for specific model, set related xxx_model_kwargs
        num_steps=50,
        num_training_steps=1000,
        ddim_discretize="uniform",
        linear_start=0.00085,
        linear_end=0.0120,
        ddim_eta=0.0,
        name="stable_diffusion",
    ):
        if pretrained != "default":
            clip_model_kwargs.update({"pretrained": pretrained})
            unet_model_kwargs.update({"pretrained": pretrained})
            decoder_model_kwargs.update({"pretrained": pretrained})
            encoder_model_kwargs.update({"pretrained": pretrained})

        """ Dynamic input shape """
        if image_data_format() == "channels_last":
            image_shape, latents_input_shape = [None, None, 3], [None, None, 4]  # [512, 512, 3] -> [64, 64, 4]
        else:
            image_shape, latents_input_shape = [3, None, None], [4, None, None]  # [3, 512, 512] -> [4, 64, 64]
        self.image_shape = (None, *image_shape)
        self.latents_input_shape = (None, *latents_input_shape)

        """ tokenizer """
        if isinstance(caption_tokenizer, str) and hasattr(tokenizer, caption_tokenizer):
            self.caption_tokenizer = getattr(tokenizer, caption_tokenizer)()
        elif isinstance(caption_tokenizer, str):  # tiktoken one
            self.caption_tokenizer = tokenizer.TikToken(caption_tokenizer)
        else:
            self.caption_tokenizer = tokenizer

        """ clip_model """
        clip_model_kwargs.update({"vocab_size": self.caption_tokenizer.vocab_size, "include_top": False})
        self.clip_model = self.build_model(clip_model, **clip_model_kwargs)
        self.uncond_prompt = None  # Init later after load weights

        """ encoder, could be None """
        encoder_model_kwargs["input_shape"] = encoder_model_kwargs.pop("input_shape", image_shape)
        self.encoder_model = self.build_model(encoder_model, **encoder_model_kwargs)
        self.encoder_model_kwargs = encoder_model_kwargs  # In case need this later, when calling image_to_image

        """ unet """
        unet_model_kwargs["input_shape"] = unet_model_kwargs.pop("input_shape", latents_input_shape)
        self.unet_model = self.build_model(unet_model, **unet_model_kwargs)

        """ decoder """
        decoder_model_kwargs["input_shape"] = decoder_model_kwargs.pop("input_shape", latents_input_shape)
        self.decoder_model = self.build_model(decoder_model, **decoder_model_kwargs)
        self.output_shape = self.decoder_model.output_shape

        """ Gather models and sampler """
        self.models = [self.clip_model, self.unet_model, self.decoder_model] + ([] if self.encoder_model is None else [self.encoder_model])
        self.num_steps, self.num_training_steps, self.ddim_discretize, self.ddim_eta = num_steps, num_training_steps, ddim_discretize, ddim_eta
        self.linear_start, self.linear_end = linear_start, linear_end
        self.init_ddim_sampler(num_steps, num_training_steps, ddim_discretize, linear_start, linear_end, ddim_eta)
        super().__init__(self.models, name=name)

    def build_model(self, model, **model_kwargs):
        if not isinstance(model, str) or len(model) == 0:
            return model

        model_split = model.split(".")
        if len(model_split) == 2:
            model_class = getattr(getattr(keras_cv_attention_models, model_split[0]), model_split[1])
        else:
            model_class = getattr(keras_cv_attention_models.models, model_split[0])
        return model_class(**model_kwargs)

    def init_ddim_sampler(self, num_steps=50, num_training_steps=1000, ddim_discretize="uniform", linear_start=0.00085, linear_end=0.0120, ddim_eta=0.0):
        # DDIM sampling from the paper [Denoising Diffusion Implicit Models](https://papers.labml.ai/paper/2010.02502)
        # num_steps, num_training_steps, ddim_discretize, linear_start, linear_end, ddim_eta = 50, 1000, "uniform", 0.00085, 0.0120, 0
        if ddim_discretize == "quad":
            time_steps = ((np.linspace(0, np.sqrt(num_training_steps * 0.8), num_steps)) ** 2).astype(int) + 1
        else:  # "uniform"
            interval = num_training_steps // num_steps
            time_steps = np.arange(0, num_training_steps, interval) + 1

        beta = np.linspace(linear_start**0.5, linear_end**0.5, num_training_steps, dtype="float64") ** 2
        alpha_bar = np.cumprod(1.0 - beta, axis=0).astype("float32")

        ddim_alpha = alpha_bar[time_steps]
        ddim_alpha_prev = np.concatenate([alpha_bar[:1], alpha_bar[time_steps[:-1]]])
        ddim_sigma = ddim_eta * ((1 - ddim_alpha_prev) / (1 - ddim_alpha) * (1 - ddim_alpha / ddim_alpha_prev)) ** 0.5

        self.time_steps, self.ddim_alpha, self.ddim_alpha_prev, self.ddim_sigma = time_steps, ddim_alpha, ddim_alpha_prev, ddim_sigma
        self.ddim_alpha_sqrt, self.ddim_sqrt_one_minus_alpha = ddim_alpha**0.5, (1.0 - ddim_alpha) ** 0.5

    def gaussian_sample(self, shape, dtype="float32", device=None):
        gaussian = np.random.normal(size=shape)
        gaussian = functional.convert_to_tensor(gaussian.astype("float32"), dtype=dtype)
        return gaussian.to(device) if backend.is_torch_backend else gaussian

    def text_to_image(
        self,
        prompt,
        batch_size=1,
        image_shape=(512, 512),  # int or list of 2 int, should be divisible by 64
        repeat_noise=False,  # specified whether the noise should be same for all samples in the batch
        temperature=1,  # is the noise temperature (random noise gets multiplied by this)
        init_x0=None,  # If not provided random noise will be used.
        init_step=0,  # is the number of time steps to skip $i'$. We start sampling from $S - i'$. And `x_last` is then $x_{\tau_{S - i'}}$.
        latent_scaling_factor=0.18215,  # scaling factor for the image latent space. Encoder outputs and Decoder inputs are scaled by this value
        uncond_scale=7.5,  # unconditional guidance scale: "eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))"
        return_inner=False,  # boolean value if return inner step results for visualizing the process
        inpaint_orign_image_latents=None,  # [in_paint parameters], not None value for inpaint mode
        inpaint_orign_noise=None,  # [in_paint parameters]
        inpaint_mask=None,  # [in_paint parameters]
    ):
        device = next(self.unet_model.parameters()).device if backend.is_torch_backend else None
        compute_dtype = self.unet_model.compute_dtype

        batch_size = batch_size if init_x0 is None else init_x0.shape[0]
        image_shape = image_shape if isinstance(image_shape, (list, tuple)) else [image_shape, image_shape]
        if image_data_format() == "channels_last":
            target_shape = [batch_size, image_shape[0] // 8, image_shape[1] // 8, self.latents_input_shape[-1]]
        else:
            target_shape = [batch_size, self.latents_input_shape[1], image_shape[-2] // 8, image_shape[-1] // 8]

        if self.uncond_prompt is None:
            uncond_prompt = functional.convert_to_tensor(self.caption_tokenizer("", padding_value=self.caption_tokenizer.eot_token)[None], dtype=compute_dtype)
            uncond_prompt = uncond_prompt.long().to(device) if backend.is_torch_backend else uncond_prompt
            self.uncond_token = self.clip_model(uncond_prompt)
        cond_prompt = functional.convert_to_tensor(self.caption_tokenizer(prompt, padding_value=self.caption_tokenizer.eot_token)[None], dtype=compute_dtype)
        cond_prompt = cond_prompt.long().to(device) if backend.is_torch_backend else cond_prompt
        cond_token = self.clip_model(cond_prompt)
        uncond_cond_prompt = functional.concat([self.uncond_token] * batch_size + [cond_token] * batch_size, axis=0)

        if init_x0 is None:
            xt = self.gaussian_sample(target_shape, dtype=compute_dtype, device=device)
        else:
            xt = functional.convert_to_tensor(init_x0, dtype=compute_dtype)
            xt = xt.to(device) if backend.is_torch_backend else xt

        is_inpaint = inpaint_orign_image_latents is not None
        rr = []
        for cur_step in tqdm(range(self.num_steps - init_step)[::-1]):
            time_step = functional.convert_to_tensor(np.stack([self.time_steps[cur_step]] * batch_size * 2), dtype=compute_dtype)
            time_step = time_step.to(device) if backend.is_torch_backend else time_step
            xt_inputs = functional.concat([xt, xt], axis=0)

            # get_eps
            out = self.unet_model([xt_inputs, time_step, uncond_cond_prompt])
            e_t_uncond, e_t_cond = functional.split(out, 2, axis=0)
            e_t = e_t_uncond + (e_t_cond - e_t_uncond) * uncond_scale

            # get_x_prev_and_pred_x0
            ddim_alpha_prev, ddim_sigma = self.ddim_alpha_prev[cur_step], self.ddim_sigma[cur_step]
            pred_x0 = (xt - e_t * self.ddim_sqrt_one_minus_alpha[cur_step]) / (self.ddim_alpha[cur_step] ** 0.5)  # Current prediction for x_0
            dir_xt = e_t * ((1.0 - ddim_alpha_prev - ddim_sigma**2) ** 0.5)  # Direction pointing to x_t

            noise_shape = (1, *target_shape[1:]) if repeat_noise else target_shape
            noise = 0.0 if ddim_sigma == 0 else self.gaussian_sample(noise_shape, dtype=compute_dtype, device=device)
            xt = (ddim_alpha_prev**0.5) * pred_x0 + dir_xt + ddim_sigma * temperature * noise

            if is_inpaint:  # q_sample
                orig_t = self.ddim_alpha_sqrt[cur_step] * inpaint_orign_image_latents + self.ddim_sqrt_one_minus_alpha[cur_step] * inpaint_orign_noise
                xt = orig_t * inpaint_mask + xt * (1 - inpaint_mask)  # Replace the masked area

            if return_inner:
                rr.append(xt)

        # Decode the image
        if return_inner:
            return [self.decoder_model(inner / latent_scaling_factor) for inner in tqdm(rr, "Decoding")]
        else:
            return self.decoder_model(xt / latent_scaling_factor)

    def image_to_image(
        self,
        image,
        prompt,
        batch_size=1,
        strength=0.75,  # specifies how much of the original image should not be preserved
        repeat_noise=False,  # specified whether the noise should be same for all samples in the batch
        temperature=1,  # is the noise temperature (random noise gets multiplied by this)
        latent_scaling_factor=0.18215,  # scaling factor for the image latent space. Encoder outputs and Decoder inputs are scaled by this value
        uncond_scale=5.0,  # unconditional guidance scale: "eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))"
        return_inner=False,  # boolean value if return inner step results for visualizing the process
        is_inpaint=False,  # [in_paint parameters] True for in_paint mode
        inpaint_mask=[0.5, 0, 1, 1],  # [in_paint parameters], list of 4 float in [0, 1], format in [top, left, bottom, right], default one for bottom half
    ):
        device = next(self.unet_model.parameters()).device if backend.is_torch_backend else None
        compute_dtype = self.unet_model.compute_dtype

        if self.encoder_model is None:
            print(">>>> Build default Encoder model: stable_diffusion.Encoder")
            encoder_model = self.build_model("stable_diffusion.Encoder", **self.encoder_model_kwargs)
            self.encoder_model = encoder_model.to(device) if backend.is_torch_backend else encoder_model

        """ Load image """
        if isinstance(image, str):
            image = Image.open(image).convert("RGB")
            width, height = image.size[0] - image.size[0] % 64, image.size[1] - image.size[1] % 64  # or 32 [???]
            image = np.array(image.resize((width, height), resample=Image.Resampling.BICUBIC))
        else:  # np.array inputs
            height, width = image.shape[0] - image.shape[0] % 64, image.shape[1] - image.shape[1] % 64  # or 32 [???]
            image = image[:height, :width]  # Just crop
            image = image if image.max() > 2 else image * 255  # -> [0, 255]
        print(">>>> input image.shape: {}, image.min(): {}, image.max(): {}".format(image.shape, image.min(), image.max()))

        """ Encoder """
        image = image.astype("float32")[None] / 127.5 - 1
        image = image if image_data_format() == "channels_last" else image.transpose([0, 3, 1, 2])
        image = functional.convert_to_tensor(image, dtype=compute_dtype)
        image = image.to(device) if backend.is_torch_backend else image
        encoder_outputs = self.encoder_model(image)

        """ Gaussian sampling"""
        channel_axis = -1 if backend.image_data_format() == "channels_last" else 1
        mean, log_var = functional.split(encoder_outputs, 2, axis=channel_axis)
        log_var = functional.clip_by_value(log_var, -30.0, 20.0)
        std = functional.exp(log_var * 0.5)
        gaussian = self.gaussian_sample(std.shape, dtype=compute_dtype, device=device)
        image_latents = (mean + std * gaussian) * latent_scaling_factor
        image_latents = functional.concat([image_latents] * batch_size, axis=0)

        """ q_sample """
        noise = self.gaussian_sample(image_latents.shape, dtype=compute_dtype, device=device)
        timestep_start = int(strength * self.num_steps)
        init_x0 = self.ddim_alpha_sqrt[timestep_start] * image_latents + self.ddim_sqrt_one_minus_alpha[timestep_start] * noise

        """ If inpaint mask """
        if is_inpaint and isinstance(inpaint_mask, (list, tuple)) and len(inpaint_mask) == 4:
            top, left, bottom, right = (np.array(inpaint_mask) * [height, width, height, width]).astype("int64")
            inpaint_mask = np.zeros(image_latents.shape).astype("float32")
            inpaint_mask[:, top:bottom, left:right, :] = 1
            inpaint_mask = functional.convert_to_tensor(inpaint_mask.astype("float32"), dtype=compute_dtype)
            inpaint_mask = inpaint_mask.to(device) if backend.is_torch_backend else inpaint_mask
        elif is_inpaint:
            assert inpaint_mask.shape == image_latents.shape, "provided inpaint_mask not in same shape with image_latents: {}".format(image_latents.shape)
            inpaint_mask = functional.convert_to_tensor(inpaint_mask.astype("float32"), dtype=compute_dtype)
            inpaint_mask = inpaint_mask.to(device) if backend.is_torch_backend else inpaint_mask

        """ p_sample """
        return self.text_to_image(
            prompt=prompt,
            batch_size=batch_size,
            image_shape=(height, width),
            repeat_noise=repeat_noise,
            temperature=temperature,
            init_x0=init_x0,
            init_step=self.num_steps - timestep_start,
            latent_scaling_factor=latent_scaling_factor,
            uncond_scale=uncond_scale,
            return_inner=return_inner,
            inpaint_orign_image_latents=image_latents if is_inpaint else None,
            inpaint_orign_noise=noise if is_inpaint else None,
            inpaint_mask=inpaint_mask if is_inpaint else None,
        )

    def in_paint(
        self,
        image,
        prompt,
        batch_size=1,
        strength=0.75,  # specifies how much of the original image should not be preserved
        repeat_noise=False,  # specified whether the noise should be same for all samples in the batch
        temperature=1,  # is the noise temperature (random noise gets multiplied by this)
        latent_scaling_factor=0.18215,  # scaling factor for the image latent space. Encoder outputs and Decoder inputs are scaled by this value
        uncond_scale=5.0,  # unconditional guidance scale: "eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))"
        return_inner=False,  # boolean value if return inner step results for visualizing the process
        inpaint_mask=[0.5, 0, 1, 1],  # in same shape with image_latents, or list of 4 float [top, left, bottom, right] in [0, 1], defualt one for bottom half
    ):
        local_kwargs = {kk: vv for kk, vv in locals().items() if kk != "self"}
        return self.image_to_image(**local_kwargs, is_inpaint=True)
