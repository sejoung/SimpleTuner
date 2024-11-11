import torch
import os
import wandb
import logging
import numpy as np
from tqdm import tqdm
from helpers.training.wrappers import unwrap_model
from PIL import Image
from helpers.training.state_tracker import StateTracker
from helpers.models.sdxl.pipeline import (
    StableDiffusionXLPipeline,
    StableDiffusionXLImg2ImgPipeline,
)
from helpers.legacy.pipeline import StableDiffusionPipeline
from diffusers.schedulers import (
    EulerDiscreteScheduler,
    EulerAncestralDiscreteScheduler,
    FlowMatchEulerDiscreteScheduler,
    UniPCMultistepScheduler,
    DDIMScheduler,
    DDPMScheduler,
)
from diffusers.utils.torch_utils import is_compiled_module
from helpers.multiaspect.image import MultiaspectImage
from helpers.image_manipulation.brightness import calculate_luminance
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("SIMPLETUNER_LOG_LEVEL") or "INFO")

try:
    from helpers.models.sd3.pipeline import (
        StableDiffusion3Pipeline,
        StableDiffusion3Img2ImgPipeline,
    )
except ImportError:
    logger.error(
        "Stable Diffusion 3 not available in this release of Diffusers. Please upgrade."
    )
    raise ImportError()

SCHEDULER_NAME_MAP = {
    "euler": EulerDiscreteScheduler,
    "euler-a": EulerAncestralDiscreteScheduler,
    "flow-match": FlowMatchEulerDiscreteScheduler,
    "unipc": UniPCMultistepScheduler,
    "ddim": DDIMScheduler,
    "ddpm": DDPMScheduler,
}

import logging
import os
import time
from diffusers.utils import is_wandb_available
from helpers.prompts import PromptHandler
from diffusers import (
    AutoencoderKL,
    DDIMScheduler,
)

if is_wandb_available():
    import wandb


logger = logging.getLogger("validation")
logger.setLevel(os.environ.get("SIMPLETUNER_LOG_LEVEL") or "INFO")


def resize_validation_images(validation_images, edge_length):
    # we have to scale all the inputs to a stage4 image down to 64px smaller edge.
    resized_validation_samples = []
    for _sample in validation_images:
        validation_shortname, validation_prompt, training_sample_image = _sample
        resize_to, crop_to, new_aspect_ratio = (
            MultiaspectImage.calculate_new_size_by_pixel_edge(
                aspect_ratio=MultiaspectImage.calculate_image_aspect_ratio(
                    training_sample_image
                ),
                resolution=int(edge_length),
                original_size=training_sample_image.size,
            )
        )
        # we can be less precise here
        training_sample_image = training_sample_image.resize(crop_to)
        resized_validation_samples.append(
            (validation_shortname, validation_prompt, training_sample_image)
        )
    return resized_validation_samples


def retrieve_validation_images():
    """
    From each data backend, collect the top 5 images for validation, such that
    we select the same images on each startup, unless the dataset changes.

    Returns:
        dict: A dictionary of shortname to image paths.
    """
    args = StateTracker.get_args()
    data_backends = StateTracker.get_data_backends(
        _type="conditioning" if args.controlnet else "image"
    )
    validation_data_backend_id = args.eval_dataset_id
    validation_set = []
    logger.info("Collecting validation images")
    for _data_backend in data_backends:
        data_backend = StateTracker.get_data_backend(_data_backend)
        data_backend_config = data_backend.get("config", {})
        should_skip_dataset = data_backend_config.get("disable_validation", False)
        logger.debug(f"Backend {_data_backend}: {data_backend}")
        if "id" not in data_backend or (
            args.controlnet and data_backend.get("dataset_type", None) != "conditioning"
        ):
            logger.debug(
                f"Skipping data backend: {_data_backend} dataset_type {data_backend.get('dataset_type', None)}"
            )
            continue
        logger.debug(f"Checking data backend: {data_backend['id']}")
        if (
            validation_data_backend_id is not None
            and data_backend["id"] != validation_data_backend_id
        ) or should_skip_dataset:
            logger.warning(f"Not collecting images from {data_backend['id']}")
            continue
        if "sampler" in data_backend:
            validation_samples_from_sampler = data_backend[
                "sampler"
            ].retrieve_validation_set(batch_size=args.num_eval_images)
            if "stage2" in args.model_type:
                validation_samples_from_sampler = resize_validation_images(
                    validation_samples_from_sampler, edge_length=64
                )

            validation_set.extend(validation_samples_from_sampler)
        else:
            logger.warning(
                f"Data backend {data_backend['id']} does not have a sampler. Skipping."
            )
    return validation_set


def prepare_validation_prompt_list(args, embed_cache):
    validation_negative_prompt_embeds = None
    validation_negative_pooled_embeds = None
    validation_prompts = (
        [""] if not StateTracker.get_args().validation_disable_unconditional else []
    )
    validation_shortnames = (
        ["unconditional"]
        if not StateTracker.get_args().validation_disable_unconditional
        else []
    )
    if not hasattr(embed_cache, "model_type"):
        raise ValueError(
            f"The default text embed cache backend was not found. You must specify 'default: true' on your text embed data backend via {StateTracker.get_args().data_backend_config}."
        )
    model_type = embed_cache.model_type
    validation_sample_images = None
    if (
        "deepfloyd-stage2" in args.model_type
        or args.controlnet
        or args.validation_using_datasets
    ):
        # Now, we prepare the DeepFloyd upscaler image inputs so that we can calculate their prompts.
        # If we don't do it here, they won't be available at inference time.
        validation_sample_images = retrieve_validation_images()
        if len(validation_sample_images) > 0:
            StateTracker.set_validation_sample_images(validation_sample_images)
            # Collect the prompts for the validation images.
            for _validation_sample in tqdm(
                validation_sample_images,
                ncols=100,
                desc="Precomputing validation image embeds",
            ):
                _, validation_prompt, _ = _validation_sample
                embed_cache.compute_embeddings_for_prompts(
                    [validation_prompt], load_from_cache=False
                )
            time.sleep(5)

    if args.validation_prompt_library:
        # Use the SimpleTuner prompts library for validation prompts.
        from helpers.prompts import prompts as prompt_library

        # Iterate through the prompts with a progress bar
        for shortname, prompt in tqdm(
            prompt_library.items(),
            leave=False,
            ncols=100,
            desc="Precomputing validation prompt embeddings",
        ):
            embed_cache.compute_embeddings_for_prompts(
                [prompt], is_validation=True, load_from_cache=False
            )
            validation_prompts.append(prompt)
            validation_shortnames.append(shortname)
    if args.user_prompt_library is not None:
        user_prompt_library = PromptHandler.load_user_prompts(args.user_prompt_library)
        for shortname, prompt in tqdm(
            user_prompt_library.items(),
            leave=False,
            ncols=100,
            desc="Precomputing user prompt library embeddings",
        ):
            embed_cache.compute_embeddings_for_prompts(
                [prompt], is_validation=True, load_from_cache=False
            )
            validation_prompts.append(prompt)
            validation_shortnames.append(shortname)
    if args.validation_prompt is not None:
        # Use a single prompt for validation.
        # This will add a single prompt to the prompt library, if in use.
        validation_prompts = validation_prompts + [args.validation_prompt]
        validation_shortnames = validation_shortnames + ["validation"]
        embed_cache.compute_embeddings_for_prompts(
            [args.validation_prompt], is_validation=True, load_from_cache=False
        )

    # Compute negative embed for validation prompts, if any are set.
    if validation_prompts:
        logger.info("Precomputing the negative prompt embed for validations.")
        if model_type == "sdxl" or model_type == "sd3" or model_type == "kolors":
            (
                validation_negative_prompt_embeds,
                validation_negative_pooled_embeds,
            ) = embed_cache.compute_embeddings_for_prompts(
                [StateTracker.get_args().validation_negative_prompt],
                is_validation=True,
                load_from_cache=False,
            )
            return (
                validation_prompts,
                validation_shortnames,
                validation_negative_prompt_embeds,
                validation_negative_pooled_embeds,
            )
        elif model_type == "legacy":
            validation_negative_prompt_embeds = (
                embed_cache.compute_embeddings_for_prompts(
                    [StateTracker.get_args().validation_negative_prompt],
                    load_from_cache=False,
                )
            )

            return (
                validation_prompts,
                validation_shortnames,
                validation_negative_prompt_embeds,
                None,
            )
        elif model_type == "pixart_sigma" or model_type == "smoldit":
            # we use the legacy encoder but we return no pooled embeds.
            validation_negative_prompt_embeds = (
                embed_cache.compute_embeddings_for_prompts(
                    [StateTracker.get_args().validation_negative_prompt],
                    load_from_cache=False,
                )
            )

            return (
                validation_prompts,
                validation_shortnames,
                validation_negative_prompt_embeds,
                None,
            )
        elif model_type == "flux":
            (
                validation_negative_prompt_embeds,
                validation_negative_pooled_embeds,
                validation_negative_time_ids,
                _,
            ) = embed_cache.compute_embeddings_for_prompts(
                [StateTracker.get_args().validation_negative_prompt],
                load_from_cache=False,
            )
            return (
                validation_prompts,
                validation_shortnames,
                validation_negative_prompt_embeds,
                validation_negative_pooled_embeds,
                validation_negative_time_ids,
            )
        else:
            raise ValueError(f"Unknown model type '{model_type}'")


def parse_validation_resolution(input_str: str) -> tuple:
    """
    If the args.validation_resolution:
     - is an int, we'll treat it as height and width square aspect
     - if it has an x in it, we will split and treat as WIDTHxHEIGHT
     - if it has comma, we will split and treat each value as above
    """
    if isinstance(input_str, int) or input_str.isdigit():
        if (
            "deepfloyd-stage2" in StateTracker.get_args().model_type
            and int(input_str) < 256
        ):
            raise ValueError(
                "Cannot use less than 256 resolution for DeepFloyd stage 2."
            )
        return (input_str, input_str)
    if "x" in input_str:
        pieces = input_str.split("x")
        if "deepfloyd-stage2" in StateTracker.get_args().model_type and (
            int(pieces[0]) < 256 or int(pieces[1]) < 256
        ):
            raise ValueError(
                "Cannot use less than 256 resolution for DeepFloyd stage 2."
            )
        return (int(pieces[0]), int(pieces[1]))


def get_validation_resolutions():
    """
    If the args.validation_resolution:
     - is an int, we'll treat it as height and width square aspect
     - if it has an x in it, we will split and treat as WIDTHxHEIGHT
     - if it has comma, we will split and treat each value as above
    """
    validation_resolution_parameter = StateTracker.get_args().validation_resolution
    if (
        type(validation_resolution_parameter) is str
        and "," in validation_resolution_parameter
    ):
        return [
            parse_validation_resolution(res)
            for res in validation_resolution_parameter.split(",")
        ]
    return [parse_validation_resolution(validation_resolution_parameter)]


def get_validation_resolutions():
    """
    If the args.validation_resolution:
     - is an int, we'll treat it as height and width square aspect
     - if it has an x in it, we will split and treat as WIDTHxHEIGHT
     - if it has comma, we will split and treat each value as above
    """
    validation_resolution_parameter = StateTracker.get_args().validation_resolution
    if (
        type(validation_resolution_parameter) is str
        and "," in validation_resolution_parameter
    ):
        return [
            parse_validation_resolution(res)
            for res in validation_resolution_parameter.split(",")
        ]
    return [parse_validation_resolution(validation_resolution_parameter)]


def parse_validation_resolution(input_str: str) -> tuple:
    """
    If the args.validation_resolution:
     - is an int, we'll treat it as height and width square aspect
     - if it has an x in it, we will split and treat as WIDTHxHEIGHT
     - if it has comma, we will split and treat each value as above
    """
    is_df_ii = (
        True if "deepfloyd-stage2" in StateTracker.get_args().model_type else False
    )
    if isinstance(input_str, int) or input_str.isdigit():
        if is_df_ii and int(input_str) < 256:
            raise ValueError(
                "Cannot use less than 256 resolution for DeepFloyd stage 2."
            )
        return (input_str, input_str)
    if "x" in input_str:
        pieces = input_str.split("x")
        if is_df_ii and (int(pieces[0]) < 256 or int(pieces[1]) < 256):
            raise ValueError(
                "Cannot use less than 256 resolution for DeepFloyd stage 2."
            )
        return (int(pieces[0]), int(pieces[1]))


class Validation:
    def __init__(
        self,
        accelerator,
        unet,
        transformer,
        args,
        validation_prompts,
        validation_shortnames,
        text_encoder_1,
        tokenizer,
        vae_path,
        weight_dtype,
        embed_cache,
        validation_negative_pooled_embeds,
        validation_negative_prompt_embeds,
        text_encoder_2,
        tokenizer_2,
        ema_model,
        vae,
        controlnet=None,
        text_encoder_3=None,
        tokenizer_3=None,
        is_deepspeed: bool = False,
    ):
        self.accelerator = accelerator
        self.prompt_handler = None
        self.unet = unet
        self.transformer = transformer
        self.controlnet = controlnet
        self.args = args
        self.save_dir = os.path.join(args.output_dir, "validation_images")
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir, exist_ok=True)
        self.global_step = None
        self.global_resume_step = None
        self.text_encoder_1 = text_encoder_1
        self.tokenizer_1 = tokenizer
        self.text_encoder_2 = text_encoder_2
        self.tokenizer_2 = tokenizer_2
        self.vae_path = vae_path
        self.validation_prompts = validation_prompts
        self.validation_shortnames = validation_shortnames
        self.validation_images = None
        self.weight_dtype = weight_dtype
        self.embed_cache = embed_cache
        self.validation_negative_prompt_mask = None
        self.validation_negative_pooled_embeds = validation_negative_pooled_embeds
        self.validation_negative_prompt_embeds = (
            validation_negative_prompt_embeds
            if (
                type(validation_negative_prompt_embeds) is not list
                and type(validation_negative_prompt_embeds) is not tuple
            )
            else validation_negative_prompt_embeds[0]
        )
        self.ema_model = ema_model
        self.vae = vae
        self.pipeline = None
        self.deepfloyd = True if "deepfloyd" in self.args.model_type else False
        self.deepfloyd_stage2 = (
            True if "deepfloyd-stage2" in self.args.model_type else False
        )
        self._discover_validation_input_samples()
        self.validation_resolutions = (
            get_validation_resolutions() if not self.deepfloyd_stage2 else ["base-256"]
        )
        self.text_encoder_3 = text_encoder_3
        self.tokenizer_3 = tokenizer_3
        self.flow_matching = (
            self.args.model_family == "sd3"
            and self.args.flow_matching_loss != "diffusion"
        ) or self.args.model_family == "flux"
        self.deepspeed = is_deepspeed
        self.inference_device = (
            accelerator.device
            if not is_deepspeed
            else "cuda" if torch.cuda.is_available() else "cpu"
        )

        self._update_state()

    def _validation_seed_source(self):
        if self.args.validation_seed_source == "gpu":
            return self.inference_device
        elif self.args.validation_seed_source == "cpu":
            return "cpu"
        else:
            raise Exception("Unknown validation seed source. Options: cpu, gpu")

    def _get_generator(self):
        _validation_seed_source = self._validation_seed_source()
        _generator = torch.Generator(device=_validation_seed_source).manual_seed(
            self.args.validation_seed or self.args.seed or 0
        )
        return _generator

    def clear_text_encoders(self):
        """
        Sets all text encoders to None.

        Returns:
            None
        """
        self.text_encoder_1 = None
        self.text_encoder_2 = None
        self.text_encoder_3 = None

    def init_vae(self):

        args = StateTracker.get_args()
        vae_path = (
            args.pretrained_model_name_or_path
            if args.pretrained_vae_model_name_or_path is None
            else args.pretrained_vae_model_name_or_path
        )
        precached_vae = StateTracker.get_vae()
        logger.debug(
            f"Was the VAE loaded? {precached_vae if precached_vae is None else 'Yes'}"
        )
        self.vae = precached_vae or AutoencoderKL.from_pretrained(
            vae_path,
            subfolder="vae" if args.pretrained_vae_model_name_or_path is None else None,
            revision=args.revision,
            force_upcast=False,
        ).to(self.inference_device)
        StateTracker.set_vae(self.vae)

        return self.vae

    def _discover_validation_input_samples(self):
        """
        If we have some workflow that requires image inputs for validation, we'll bind those now.

        Returns:
            Validation object (self)
        """
        self.validation_image_inputs = None
        if (
            self.deepfloyd_stage2
            or self.args.validation_using_datasets
            or self.args.controlnet
        ):
            self.validation_image_inputs = retrieve_validation_images()
            # Validation inputs are in the format of a list of tuples:
            # [(shortname, prompt, image), ...]
            logger.debug(
                f"Image inputs discovered for validation: {self.validation_image_inputs}"
            )

    def _pipeline_cls(self):
        model_type = StateTracker.get_model_family()
        if model_type == "sdxl":
            if self.args.controlnet:
                from diffusers.pipelines import StableDiffusionXLControlNetPipeline

                return StableDiffusionXLControlNetPipeline
            if self.args.validation_using_datasets:
                return StableDiffusionXLImg2ImgPipeline
            return StableDiffusionXLPipeline
        elif model_type == "flux":
            from helpers.models.flux import FluxPipeline

            if self.args.controlnet:
                raise NotImplementedError("Flux ControlNet is not yet supported.")
            if self.args.validation_using_datasets:
                raise NotImplementedError(
                    "Flux inference validation using img2img is not yet supported. Please remove --validation_using_datasets."
                )
            return FluxPipeline
        elif model_type == "kolors":
            if self.args.controlnet:
                raise NotImplementedError("Kolors ControlNet is not yet supported.")
            if self.args.validation_using_datasets:
                try:
                    from helpers.kolors.pipeline import KolorsImg2ImgPipeline
                except:
                    logger.error(
                        "Kolors pipeline requires the latest version of Diffusers."
                    )
                return KolorsImg2ImgPipeline
            try:
                from helpers.kolors.pipeline import KolorsPipeline
            except Exception:
                logger.error(
                    "Kolors pipeline requires the latest version of Diffusers."
                )
            return KolorsPipeline
        elif model_type == "legacy":
            if self.deepfloyd_stage2:
                from diffusers.pipelines import IFSuperResolutionPipeline

                return IFSuperResolutionPipeline
            return StableDiffusionPipeline
        elif model_type == "sd3":
            if self.args.controlnet:
                raise Exception("SD3 ControlNet is not yet supported.")
            if self.args.validation_using_datasets:
                return StableDiffusion3Img2ImgPipeline
            return StableDiffusion3Pipeline
        elif model_type == "pixart_sigma":
            if self.args.controlnet:
                raise Exception(
                    "PixArt Sigma ControlNet inference validation is not yet supported."
                )
            if self.args.validation_using_datasets:
                raise Exception(
                    "PixArt Sigma inference validation using img2img is not yet supported. Please remove --validation_using_datasets."
                )
            from helpers.models.pixart.pipeline import PixArtSigmaPipeline

            return PixArtSigmaPipeline
        elif model_type == "smoldit":
            from helpers.models.smoldit import SmolDiTPipeline

            return SmolDiTPipeline
        else:
            raise NotImplementedError(
                f"Model type {model_type} not implemented for validation."
            )

    def _gather_prompt_embeds(self, validation_prompt: str):
        prompt_embeds = {}
        current_validation_prompt_mask = None
        if (
            StateTracker.get_model_family() == "sdxl"
            or StateTracker.get_model_family() == "sd3"
            or StateTracker.get_model_family() == "kolors"
            or StateTracker.get_model_family() == "flux"
        ):
            _embed = self.embed_cache.compute_embeddings_for_prompts(
                [validation_prompt]
            )
            current_validation_time_ids = None
            if len(_embed) == 2:
                (
                    current_validation_prompt_embeds,
                    current_validation_pooled_embeds,
                ) = _embed
            elif len(_embed) == 3:
                (
                    current_validation_prompt_embeds,
                    current_validation_pooled_embeds,
                    current_validation_time_ids,
                ) = _embed
            elif len(_embed) == 4:
                (
                    current_validation_prompt_embeds,
                    current_validation_pooled_embeds,
                    current_validation_time_ids,
                    current_validation_prompt_mask,
                ) = _embed
            else:
                raise ValueError(
                    f"Unexpected number of embeddings returned from cache: {_embed}"
                )
            current_validation_pooled_embeds = current_validation_pooled_embeds.to(
                device=self.inference_device, dtype=self.weight_dtype
            )
            if current_validation_time_ids is not None:
                current_validation_time_ids = current_validation_time_ids.to(
                    device=self.inference_device, dtype=self.weight_dtype
                )
            self.validation_negative_pooled_embeds = (
                self.validation_negative_pooled_embeds.to(
                    device=self.inference_device, dtype=self.weight_dtype
                )
            )
            prompt_embeds["pooled_prompt_embeds"] = current_validation_pooled_embeds.to(
                device=self.inference_device, dtype=self.weight_dtype
            )
            prompt_embeds["negative_pooled_prompt_embeds"] = (
                self.validation_negative_pooled_embeds
            )
            # if current_validation_time_ids is not None:
            #     prompt_embeds["time_ids"] = current_validation_time_ids
        elif (
            StateTracker.get_model_family() == "legacy"
            or StateTracker.get_model_family() == "pixart_sigma"
            or StateTracker.get_model_family() == "smoldit"
        ):
            self.validation_negative_pooled_embeds = None
            current_validation_pooled_embeds = None
            current_validation_prompt_embeds = (
                self.embed_cache.compute_embeddings_for_prompts([validation_prompt])
            )
            if StateTracker.get_model_family() in ["pixart_sigma", "smoldit"]:
                current_validation_prompt_embeds, current_validation_prompt_mask = (
                    current_validation_prompt_embeds
                )
                current_validation_prompt_embeds = current_validation_prompt_embeds[
                    0
                ].to(device=self.inference_device, dtype=self.weight_dtype)
                if (
                    type(self.validation_negative_prompt_embeds) is tuple
                    or type(self.validation_negative_prompt_embeds) is list
                ):
                    (
                        self.validation_negative_prompt_embeds,
                        self.validation_negative_prompt_mask,
                    ) = self.validation_negative_prompt_embeds[0]
            else:
                current_validation_prompt_embeds = current_validation_prompt_embeds[
                    0
                ].to(device=self.inference_device, dtype=self.weight_dtype)
            # logger.debug(
            #     f"Validations received the prompt embed: ({type(current_validation_prompt_embeds)}) positive={current_validation_prompt_embeds.shape if type(current_validation_prompt_embeds) is not list else current_validation_prompt_embeds[0].shape},"
            #     f" ({type(self.validation_negative_prompt_embeds)}) negative={self.validation_negative_prompt_embeds.shape if type(self.validation_negative_prompt_embeds) is not list else self.validation_negative_prompt_embeds[0].shape}"
            # )
            # logger.debug(
            #     f"Dtypes: {current_validation_prompt_embeds.dtype}, {self.validation_negative_prompt_embeds.dtype}"
            # )
        else:
            raise NotImplementedError(
                f"Model type {StateTracker.get_model_family()} not implemented for validation."
            )

        current_validation_prompt_embeds = current_validation_prompt_embeds.to(
            device=self.inference_device, dtype=self.weight_dtype
        )
        self.validation_negative_prompt_embeds = (
            self.validation_negative_prompt_embeds.to(
                device=self.inference_device, dtype=self.weight_dtype
            )
        )
        # when sampling unconditional guidance, you should only zero one or the other prompt, and not both.
        # we'll assume that the user has a negative prompt, so that the unconditional sampling works.
        # the positive prompt embed is zeroed out for SDXL at the time of it being placed into the cache.
        # the embeds are not zeroed out for any other model, including Stable Diffusion 3.
        prompt_embeds["prompt_embeds"] = current_validation_prompt_embeds
        prompt_embeds["negative_prompt_embeds"] = self.validation_negative_prompt_embeds
        if (
            StateTracker.get_model_family() == "pixart_sigma"
            or StateTracker.get_model_family() == "smoldit"
            or (
                StateTracker.get_model_family() == "flux"
                and StateTracker.get_args().flux_attention_masked_training
            )
        ):
            logger.debug(
                f"mask: {current_validation_prompt_mask.shape if type(current_validation_prompt_mask) is torch.Tensor else None}"
            )
            assert current_validation_prompt_mask is not None
            prompt_embeds["prompt_mask"] = current_validation_prompt_mask
            prompt_embeds["negative_mask"] = self.validation_negative_prompt_mask

        return prompt_embeds

    def _benchmark_path(self, benchmark: str = "base_model"):
        # does the benchmark directory exist?
        if not os.path.exists(os.path.join(self.args.output_dir, "benchmarks")):
            os.makedirs(os.path.join(self.args.output_dir, "benchmarks"), exist_ok=True)
        return os.path.join(self.args.output_dir, "benchmarks", benchmark)

    def stitch_benchmark_image(
        self, validation_image_result, benchmark_image, separator_width=5
    ):
        """
        For each image, make a new canvas and place it side by side with its equivalent from {self.validation_image_inputs}
        Add "base model" text to the left image and "checkpoint" text to the right image
        Include a separator between the images
        """

        # Calculate new dimensions
        new_width = validation_image_result.size[0] * 2 + separator_width
        new_height = validation_image_result.size[1]

        # Create a new image with a white background
        new_image = Image.new("RGB", (new_width, new_height), color="white")

        # Paste the images with a gap between them
        new_image.paste(benchmark_image, (0, 0))
        new_image.paste(
            validation_image_result, (benchmark_image.size[0] + separator_width, 0)
        )

        # Create a drawing object
        draw = ImageDraw.Draw(new_image)

        # Use a default font
        try:
            font = ImageFont.truetype("arial.ttf", 36)
        except IOError:
            font = ImageFont.load_default()

        # Add text to the left image
        draw.text(
            (10, 10),
            "base model",
            fill=(255, 255, 255),
            font=font,
            stroke_width=2,
            stroke_fill=(0, 0, 0),
        )

        # Add text to the right image
        draw.text(
            (validation_image_result.size[0] + separator_width + 10, 10),
            "checkpoint",
            fill=(255, 255, 255),
            font=font,
            stroke_width=2,
            stroke_fill=(0, 0, 0),
        )

        # Draw a vertical line as a separator
        line_color = (200, 200, 200)  # Light gray
        for i in range(separator_width):
            x = validation_image_result.size[0] + i
            draw.line([(x, 0), (x, new_height)], fill=line_color)

        return new_image

    def _benchmark_image(self, shortname, resolution):
        """
        We will retrieve the benchmark image for the shortname.
        """
        if not self.benchmark_exists():
            return None
        base_model_benchmark = self._benchmark_path("base_model")
        benchmark_image = None
        _test_filename = f"{shortname}_{resolution[0]}x{resolution[1]}.png"
        for _benchmark_image in os.listdir(base_model_benchmark):
            _basename = os.path.basename(_benchmark_image)
            if _basename == _test_filename:
                benchmark_image = Image.open(
                    os.path.join(base_model_benchmark, _benchmark_image)
                )
                break

        return benchmark_image

    def _benchmark_images(self):
        """
        We will retrieve the benchmark images so they can be stitched to the validation outputs.
        """
        if not self.benchmark_exists():
            return None
        benchmark_images = []
        base_model_benchmark = self._benchmark_path("base_model")
        for _benchmark_image in os.listdir(base_model_benchmark):
            if _benchmark_image.endswith(".png"):
                benchmark_images.append(
                    (
                        _benchmark_image.replace(".png", ""),
                        f"Base model benchmark image {_benchmark_image}",
                        Image.open(
                            os.path.join(base_model_benchmark, _benchmark_image)
                        ),
                    )
                )

        return benchmark_images

    def benchmark_exists(self, benchmark: str = "base_model"):
        """
        Determines whether the base model benchmark outputs already exist.
        """
        base_model_benchmark = self._benchmark_path()

        return os.path.exists(base_model_benchmark)

    def save_benchmark(self, benchmark: str = "base_model"):
        """
        Saves the benchmark outputs for the base model.
        """
        base_model_benchmark = self._benchmark_path(benchmark=benchmark)
        if not os.path.exists(base_model_benchmark):
            os.makedirs(base_model_benchmark, exist_ok=True)
        if self.validation_images is None:
            return
        for shortname, image_list in self.validation_images.items():
            for idx, image in enumerate(image_list):
                width, height = image.size
                image.save(
                    os.path.join(
                        base_model_benchmark, f"{shortname}_{width}x{height}.png"
                    )
                )

    def _update_state(self):
        """Updates internal state with the latest from StateTracker."""
        self.global_step = StateTracker.get_global_step()
        self.global_resume_step = StateTracker.get_global_resume_step() or 1

    def run_validations(
        self,
        step: int = 0,
        validation_type="intermediary",
        force_evaluation: bool = False,
        skip_execution: bool = False,
    ):
        self._update_state()
        should_validate = self.should_perform_validation(
            step, self.validation_prompts, validation_type
        ) or (step == 0 and validation_type == "base_model")
        logger.debug(
            f"Should evaluate: {should_validate}, force evaluation: {force_evaluation}, skip execution: {skip_execution}"
        )
        if not should_validate and not force_evaluation:
            return self
        if should_validate and skip_execution:
            # If the validation would have fired off, we'll skip it.
            # This is useful at the end of training so we don't validate 2x.
            return self
        if StateTracker.get_webhook_handler() is not None:
            StateTracker.get_webhook_handler().send(
                message="Validations are generating.. this might take a minute! 🖼️",
                message_level="info",
            )

        if self.accelerator.is_main_process or self.deepspeed:
            logger.debug("Starting validation process...")
            self.setup_pipeline(validation_type)
            if self.pipeline is None:
                logger.error(
                    "Not able to run validations, we did not obtain a valid pipeline."
                )
                self.validation_images = None
                return self
            self.setup_scheduler()
            self.process_prompts()
            self.finalize_validation(validation_type)
            logger.debug("Validation process completed.")
            self.clean_pipeline()

        return self

    def should_perform_validation(self, step, validation_prompts, validation_type):
        should_do_intermediary_validation = (
            validation_prompts
            and self.global_step % self.args.validation_steps == 0
            and step % self.args.gradient_accumulation_steps == 0
            and self.global_step > self.global_resume_step
        )
        is_final_validation = validation_type == "final"
        return (is_final_validation or should_do_intermediary_validation) and (
            self.accelerator.is_main_process or self.deepseed
        )

    def setup_scheduler(self):
        if self.args.validation_noise_scheduler is None:
            return
        if self.flow_matching:
            # NO TOUCHIE FOR FLOW-MATCHING.
            # Touchie for diffusion though.
            return

        scheduler_args = {}
        if (
            self.pipeline is not None
            and "variance_type" in self.pipeline.scheduler.config
        ):
            variance_type = self.pipeline.scheduler.config.variance_type

            if variance_type in ["learned", "learned_range"]:
                variance_type = "fixed_small"

            scheduler_args["variance_type"] = variance_type
        if self.deepfloyd:
            self.args.validation_noise_scheduler = "ddpm"
        scheduler = SCHEDULER_NAME_MAP[
            self.args.validation_noise_scheduler
        ].from_pretrained(
            self.args.pretrained_model_name_or_path,
            subfolder="scheduler",
            revision=self.args.revision,
            prediction_type=self.args.prediction_type,
            timestep_spacing=self.args.inference_scheduler_timestep_spacing,
            rescale_betas_zero_snr=self.args.rescale_betas_zero_snr,
            **scheduler_args,
        )
        if self.pipeline is not None:
            self.pipeline.scheduler = scheduler
        return scheduler

    def setup_pipeline(self, validation_type, enable_ema_model: bool = True):
        if validation_type == "intermediary" and self.args.use_ema:
            if enable_ema_model:
                if self.unet is not None:
                    self.ema_model.store(self.unet.parameters())
                    self.ema_model.copy_to(self.unet.parameters())
                if self.transformer is not None:
                    self.ema_model.store(self.transformer.parameters())
                    self.ema_model.copy_to(self.transformer.parameters())
                if self.args.ema_device != "accelerator":
                    logger.info("Moving EMA weights to GPU for inference.")
                    self.ema_model.to(self.inference_device)
            else:
                logger.debug(
                    "Skipping EMA model setup for validation, as enable_ema_model=False."
                )

        if self.pipeline is None:
            pipeline_cls = self._pipeline_cls()
            extra_pipeline_kwargs = {
                "text_encoder": self.text_encoder_1,
                "tokenizer": self.tokenizer_1,
                "vae": self.vae,
                "safety_checker": None,
            }
            if self.args.model_family in ["sd3", "sdxl", "flux"]:
                extra_pipeline_kwargs["text_encoder_2"] = None
            if self.args.model_family in ["sd3"]:
                extra_pipeline_kwargs["text_encoder_3"] = None
            if type(pipeline_cls) is StableDiffusionXLPipeline:
                del extra_pipeline_kwargs["safety_checker"]
                del extra_pipeline_kwargs["text_encoder"]
                del extra_pipeline_kwargs["tokenizer"]
                if validation_type == "final":
                    if self.text_encoder_1 is not None:
                        extra_pipeline_kwargs["text_encoder_1"] = unwrap_model(
                            self.accelerator, self.text_encoder_1
                        )
                        extra_pipeline_kwargs["tokenizer_1"] = self.tokenizer_1
                        if self.text_encoder_2 is not None:
                            extra_pipeline_kwargs["text_encoder_2"] = unwrap_model(
                                self.accelerator, self.text_encoder_2
                            )
                            extra_pipeline_kwargs["tokenizer_2"] = self.tokenizer_2
                else:
                    extra_pipeline_kwargs["text_encoder_1"] = None
                    extra_pipeline_kwargs["tokenizer_1"] = None
                    extra_pipeline_kwargs["text_encoder_2"] = None
                    extra_pipeline_kwargs["tokenizer_2"] = None

            if self.args.model_family == "smoldit":
                extra_pipeline_kwargs["transformer"] = unwrap_model(
                    self.accelerator, self.transformer
                )
                extra_pipeline_kwargs["tokenizer"] = self.tokenizer_1
                extra_pipeline_kwargs["text_encoder"] = self.text_encoder_1
                extra_pipeline_kwargs["scheduler"] = self.setup_scheduler()

            if self.args.controlnet:
                # ControlNet training has an additional adapter thingy.
                extra_pipeline_kwargs["controlnet"] = unwrap_model(
                    self.accelerator, self.controlnet
                )
            if self.unet is not None:
                extra_pipeline_kwargs["unet"] = unwrap_model(
                    self.accelerator, self.unet
                )

            if self.transformer is not None:
                extra_pipeline_kwargs["transformer"] = unwrap_model(
                    self.accelerator, self.transformer
                )

            if self.args.model_family == "sd3" and self.args.train_text_encoder:
                if self.text_encoder_1 is not None:
                    extra_pipeline_kwargs["text_encoder"] = unwrap_model(
                        self.accelerator, self.text_encoder_1
                    )
                    extra_pipeline_kwargs["tokenizer"] = self.tokenizer_1
                if self.text_encoder_2 is not None:
                    extra_pipeline_kwargs["text_encoder_2"] = unwrap_model(
                        self.accelerator, self.text_encoder_2
                    )
                    extra_pipeline_kwargs["tokenizer_2"] = self.tokenizer_2
                if self.text_encoder_3 is not None:
                    extra_pipeline_kwargs["text_encoder_3"] = unwrap_model(
                        self.accelerator, self.text_encoder_3
                    )
                    extra_pipeline_kwargs["tokenizer_3"] = self.tokenizer_3

            if self.vae is None or not hasattr(self.vae, "device"):
                extra_pipeline_kwargs["vae"] = self.init_vae()
            if (
                "vae" in extra_pipeline_kwargs
                and extra_pipeline_kwargs.get("vae") is not None
                and extra_pipeline_kwargs["vae"].device != self.inference_device
            ):
                extra_pipeline_kwargs["vae"] = extra_pipeline_kwargs["vae"].to(
                    self.inference_device
                )

            pipeline_kwargs = {
                "pretrained_model_name_or_path": self.args.pretrained_model_name_or_path,
                "revision": self.args.revision,
                "variant": self.args.variant,
                "torch_dtype": self.weight_dtype,
                **extra_pipeline_kwargs,
            }
            logger.debug(f"Initialising pipeline with kwargs: {pipeline_kwargs}")
            attempt = 0
            while attempt < 3:
                attempt += 1
                try:
                    if self.args.model_family == "smoldit":
                        self.pipeline = pipeline_cls(
                            vae=self.vae,
                            transformer=unwrap_model(
                                self.accelerator, self.transformer
                            ),
                            tokenizer=self.tokenizer_1,
                            text_encoder=self.text_encoder_1,
                            scheduler=self.setup_scheduler(),
                        )
                    else:
                        self.pipeline = pipeline_cls.from_pretrained(**pipeline_kwargs)
                except Exception as e:
                    import traceback

                    logger.error(e)
                    logger.error(traceback.format_exc())
                    continue
                break
            if self.args.validation_torch_compile:
                if self.deepspeed:
                    logger.warning("DeepSpeed does not support torch compile. Disabling. Set --validation_torch_compile=False to suppress this warning.")
                elif self.lora_type.lower() == "lycoris":
                    logger.warning("LyCORIS does not support torch compile for validation due to graph compile breaks. Disabling. Set --validation_torch_compile=False to suppress this warning.")
                else:
                    if self.unet is not None and not is_compiled_module(self.unet):
                        logger.warning(
                            f"Compiling the UNet for validation ({self.args.validation_torch_compile})"
                        )
                        self.pipeline.unet = torch.compile(
                            self.pipeline.unet,
                            mode=self.args.validation_torch_compile_mode,
                            fullgraph=False,
                        )
                    if self.transformer is not None and not is_compiled_module(
                        self.transformer
                    ):
                        logger.warning(
                            f"Compiling the transformer for validation ({self.args.validation_torch_compile})"
                        )
                        self.pipeline.transformer = torch.compile(
                            self.pipeline.transformer,
                            mode=self.args.validation_torch_compile_mode,
                            fullgraph=False,
                        )

        self.pipeline = self.pipeline.to(self.inference_device)
        self.pipeline.set_progress_bar_config(disable=True)

    def clean_pipeline(self):
        """Remove the pipeline."""
        if self.pipeline is not None:
            del self.pipeline
            self.pipeline = None

    def process_prompts(self):
        """Processes each validation prompt and logs the result."""
        validation_images = {}
        _content = zip(self.validation_shortnames, self.validation_prompts)
        total_samples = (
            len(self.validation_shortnames)
            if self.validation_shortnames is not None
            else 0
        )
        if self.validation_image_inputs:
            # Override the pipeline inputs to be entirely based upon the validation image inputs.
            _content = self.validation_image_inputs
            total_samples = len(_content) if _content is not None else 0
        for content in tqdm(
            _content if _content else [],
            desc="Processing validation prompts",
            total=total_samples,
            leave=False,
            position=1,
        ):
            validation_input_image = None
            logger.debug(f"content: {content}")
            if len(content) == 3:
                shortname, prompt, validation_input_image = content
            elif len(content) == 2:
                shortname, prompt = content
            else:
                raise ValueError(
                    f"Validation content is not in the correct format: {content}"
                )
            logger.debug(f"Processing validation for prompt: {prompt}")
            validation_images.update(
                self.validate_prompt(prompt, shortname, validation_input_image)
            )
            self._save_images(validation_images, shortname, prompt)
            self._log_validations_to_webhook(validation_images, shortname, prompt)
            logger.debug(f"Completed generating image: {prompt}")
        self.validation_images = validation_images
        try:
            self._log_validations_to_trackers(validation_images)
        except Exception as e:
            logger.error(f"Error logging validation images: {e}")

    def stitch_conditioning_images(self, validation_image_results, conditioning_image):
        """
        For each image, make a new canvas and place it side by side with its equivalent from {self.validation_image_inputs}
        """
        stitched_validation_images = []
        for idx, image in enumerate(validation_image_results):
            new_width = image.size[0] * 2
            new_height = image.size[1]
            new_image = Image.new("RGB", (new_width, new_height))
            new_image.paste(image, (0, 0))
            new_image.paste(conditioning_image, (image.size[0], 0))
            stitched_validation_images.append(new_image)

        return stitched_validation_images

    def validate_prompt(
        self, prompt, validation_shortname, validation_input_image=None
    ):
        """Generate validation images for a single prompt."""
        # Placeholder for actual image generation and logging
        logger.debug(f"Validating prompt: {prompt}")
        validation_images = {}
        for resolution in self.validation_resolutions:
            extra_validation_kwargs = {}
            if not self.args.validation_randomize:
                extra_validation_kwargs["generator"] = self._get_generator()
                logger.debug(
                    f"Using a generator? {extra_validation_kwargs['generator']}"
                )
            if validation_input_image is not None:
                extra_validation_kwargs["image"] = validation_input_image
                if self.deepfloyd_stage2:
                    validation_resolution_width, validation_resolution_height = (
                        val * 4 for val in extra_validation_kwargs["image"].size
                    )
                elif self.args.controlnet or self.args.validation_using_datasets:
                    validation_resolution_width, validation_resolution_height = (
                        extra_validation_kwargs["image"].size
                    )
                else:
                    raise ValueError(
                        "Validation input images are not supported for this model type."
                    )
            else:
                validation_resolution_width, validation_resolution_height = resolution

            if (
                self.args.model_family == "sd3"
                and type(self.args.validation_guidance_skip_layers) is list
            ):
                extra_validation_kwargs["skip_layer_guidance_start"] = float(
                    self.args.validation_guidance_skip_layers_start
                )
                extra_validation_kwargs["skip_layer_guidance_stop"] = float(
                    self.args.validation_guidance_skip_layers_stop
                )
                extra_validation_kwargs["skip_layer_guidance_scale"] = float(
                    self.args.validation_guidance_skip_scale
                )
                extra_validation_kwargs["skip_guidance_layers"] = list(
                    self.args.validation_guidance_skip_layers
                )

            if not self.flow_matching and self.args.model_family not in [
                "deepfloyd",
                "pixart_sigma",
                "kolors",
                "flux",
                "sd3",
            ]:
                extra_validation_kwargs["guidance_rescale"] = (
                    self.args.validation_guidance_rescale
                )

            if StateTracker.get_args().validation_using_datasets:
                extra_validation_kwargs["strength"] = getattr(
                    self.args, "validation_strength", 0.2
                )
                logger.debug(
                    f"Set validation image denoise strength to {extra_validation_kwargs['strength']}"
                )

            logger.debug(
                f"Processing width/height: {validation_resolution_width}x{validation_resolution_height}"
            )
            if validation_shortname not in validation_images:
                validation_images[validation_shortname] = []
            try:
                extra_validation_kwargs.update(self._gather_prompt_embeds(prompt))
            except Exception as e:
                import traceback

                logger.error(
                    f"Error gathering text embed for validation prompt {prompt}: {e}, traceback: {traceback.format_exc()}"
                )
                continue

            try:
                # print(f"pipeline dtype: {self.pipeline.unet.device}")
                pipeline_kwargs = {
                    "prompt": None,
                    "negative_prompt": None,
                    "num_images_per_prompt": self.args.num_validation_images,
                    "num_inference_steps": self.args.validation_num_inference_steps,
                    "guidance_scale": self.args.validation_guidance,
                    "height": MultiaspectImage._round_to_nearest_multiple(
                        int(validation_resolution_height)
                    ),
                    "width": MultiaspectImage._round_to_nearest_multiple(
                        int(validation_resolution_width)
                    ),
                    **extra_validation_kwargs,
                }
                if self.args.validation_guidance_real > 1.0:
                    pipeline_kwargs["guidance_scale_real"] = float(
                        self.args.validation_guidance_real
                    )
                if (
                    isinstance(self.args.validation_no_cfg_until_timestep, int)
                    and self.args.model_family == "flux"
                ):
                    pipeline_kwargs["no_cfg_until_timestep"] = (
                        self.args.validation_no_cfg_until_timestep
                    )

                logger.debug(
                    f"Image being generated with parameters: {pipeline_kwargs}"
                )
                # Print the device attr of any parameters that have one
                for key, value in pipeline_kwargs.items():
                    if hasattr(value, "device"):
                        logger.debug(f"Device for {key}: {value.device}")
                for key, value in self.pipeline.components.items():
                    if hasattr(value, "device"):
                        logger.debug(f"Device for {key}: {value.device}")
                if StateTracker.get_model_family() == "flux":
                    if "negative_prompt" in pipeline_kwargs:
                        del pipeline_kwargs["negative_prompt"]
                if (
                    StateTracker.get_model_family() == "pixart_sigma"
                    or StateTracker.get_model_family() == "smoldit"
                ):
                    if pipeline_kwargs.get("negative_prompt") is not None:
                        del pipeline_kwargs["negative_prompt"]
                    if pipeline_kwargs.get("prompt") is not None:
                        del pipeline_kwargs["prompt"]
                    pipeline_kwargs["prompt_attention_mask"] = pipeline_kwargs.pop(
                        "prompt_mask"
                    )[0].to(device=self.inference_device, dtype=self.weight_dtype)
                    pipeline_kwargs["negative_prompt_attention_mask"] = torch.unsqueeze(
                        pipeline_kwargs.pop("negative_mask")[0], dim=0
                    ).to(device=self.inference_device, dtype=self.weight_dtype)

                validation_image_results = self.pipeline(**pipeline_kwargs).images
                if self.args.controlnet:
                    validation_image_results = self.stitch_conditioning_images(
                        validation_image_results, extra_validation_kwargs["image"]
                    )
                elif not self.args.disable_benchmark and self.benchmark_exists(
                    "base_model"
                ):
                    benchmark_image = self._benchmark_image(
                        validation_shortname, resolution
                    )
                    if benchmark_image is not None:
                        # user might have added new resolutions or something.
                        validation_image_results[0] = self.stitch_benchmark_image(
                            validation_image_results[0], benchmark_image
                        )
                validation_images[validation_shortname].extend(validation_image_results)
            except Exception as e:
                import traceback

                logger.error(
                    f"Error generating validation image: {e}, {traceback.format_exc()}"
                )
                continue

        return validation_images

    def _save_images(self, validation_images, validation_shortname, validation_prompt):
        validation_img_idx = 0
        for validation_image in validation_images[validation_shortname]:
            res = self.validation_resolutions[validation_img_idx]
            if "x" in res:
                res_label = str(res)
            elif type(res) is tuple:
                res_label = f"{res[0]}x{res[1]}"
            else:
                res_label = f"{res}x{res}"
            validation_image.save(
                os.path.join(
                    self.save_dir,
                    f"step_{StateTracker.get_global_step()}_{validation_shortname}_{res_label}.png",
                )
            )
            validation_img_idx += 1

    def _log_validations_to_webhook(
        self, validation_images, validation_shortname, validation_prompt
    ):
        if StateTracker.get_webhook_handler() is not None:
            StateTracker.get_webhook_handler().send(
                f"Validation image for `{validation_shortname if validation_shortname != '' else '(blank shortname)'}`"
                f"\nValidation prompt: `{validation_prompt if validation_prompt != '' else '(blank prompt)'}`",
                images=validation_images[validation_shortname],
            )

    def _log_validations_to_trackers(self, validation_images):
        for tracker in self.accelerator.trackers:
            if tracker.name == "comet_ml":
                experiment = self.accelerator.get_tracker("comet_ml").tracker
                for shortname, image_list in validation_images.items():
                    for idx, image in enumerate(image_list):
                        experiment.log_image(
                            image,
                            name=f"{shortname} - {self.validation_resolutions[idx]}",
                        )
            elif tracker.name == "tensorboard":
                tracker = self.accelerator.get_tracker("tensorboard")
                for shortname, image_list in validation_images.items():
                    tracker.log_images(
                        {
                            f"{shortname} - {self.validation_resolutions[idx]}": np.moveaxis(
                                np.array(image), -1, 0
                            )[
                                np.newaxis, ...
                            ]
                            for idx, image in enumerate(image_list)
                        },
                        step=StateTracker.get_global_step(),
                    )
            elif tracker.name == "wandb":
                resolution_list = [
                    f"{res[0]}x{res[1]}" for res in get_validation_resolutions()
                ]

                if self.args.tracker_image_layout == "table":
                    columns = [
                        "Prompt",
                        *resolution_list,
                        "Mean Luminance",
                    ]
                    table = wandb.Table(columns=columns)

                    # Process each prompt and its associated images
                    for prompt_shortname, image_list in validation_images.items():
                        wandb_images = []
                        luminance_values = []
                        logger.debug(
                            f"Prompt {prompt_shortname} has {len(image_list)} images"
                        )
                        for image in image_list:
                            logger.debug(f"Adding to table: {image}")
                            wandb_image = wandb.Image(image)
                            wandb_images.append(wandb_image)
                            luminance = calculate_luminance(image)
                            luminance_values.append(luminance)
                        mean_luminance = torch.tensor(luminance_values).mean().item()
                        while len(wandb_images) < len(resolution_list):
                            # any missing images will crash it. use None so they are indexed.
                            logger.debug("Found a missing image - masking with a None")
                            wandb_images.append(None)
                        table.add_data(prompt_shortname, *wandb_images, mean_luminance)

                    # Log the table to Weights & Biases
                    tracker.log(
                        {"Validation Gallery": table},
                        step=StateTracker.get_global_step(),
                    )

                elif self.args.tracker_image_layout == "gallery":
                    gallery_images = {}
                    for prompt_shortname, image_list in validation_images.items():
                        logger.debug(
                            f"Prompt {prompt_shortname} has {len(image_list)} images"
                        )
                        for idx, image in enumerate(image_list):
                            wandb_image = wandb.Image(
                                image,
                                caption=f"{prompt_shortname} - {resolution_list[idx]}",
                            )
                            gallery_images[
                                f"{prompt_shortname} - {resolution_list[idx]}"
                            ] = wandb_image

                    # Log all images in one call to prevent the global step from ticking
                    tracker.log(gallery_images, step=StateTracker.get_global_step())

    def finalize_validation(self, validation_type, enable_ema_model: bool = True):
        """Cleans up and restores original state if necessary."""
        if validation_type == "intermediary" and self.args.use_ema:
            if enable_ema_model:
                if self.unet is not None:
                    self.ema_model.restore(self.unet.parameters())
                if self.transformer is not None:
                    self.ema_model.restore(self.transformer.parameters())
                if self.args.ema_device != "accelerator":
                    self.ema_model.to(self.args.ema_device)
            else:
                logger.debug(
                    "Skipping EMA model restoration for validation, as enable_ema_model=False."
                )
        if not self.args.keep_vae_loaded and not self.args.vae_cache_ondemand:
            self.vae = self.vae.to("cpu")
            self.vae = None
        self.pipeline = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
