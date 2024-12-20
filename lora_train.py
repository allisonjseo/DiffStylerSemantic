from timeit import default_timer as timer
from datetime import timedelta
from PIL import Image
import os
import numpy as np
from einops import rearrange
import torch
import torch.nn.functional as F
from torchvision import transforms
import transformers
from accelerate import Accelerator
from accelerate.utils import set_seed
from packaging import version
from PIL import Image
import tqdm
import argparse
from transformers import AutoTokenizer, PretrainedConfig
from torchvision import models
import torchvision.transforms as T

import diffusers
from diffusers import (
    AutoencoderKL,
    DDPMScheduler,
    DiffusionPipeline,
    DPMSolverMultistepScheduler,
    StableDiffusionPipeline,
    UNet2DConditionModel,
)
from diffusers.loaders import AttnProcsLayers, LoraLoaderMixin
from diffusers.models.attention_processor import (
    AttnAddedKVProcessor,
    AttnAddedKVProcessor2_0,
    LoRAAttnAddedKVProcessor,
    LoRAAttnProcessor,
    LoRAAttnProcessor2_0,
    SlicedAttnAddedKVProcessor,
)
from diffusers.optimization import get_scheduler
from diffusers.utils import check_min_version
from diffusers.utils.import_utils import is_xformers_available

import matplotlib.pyplot as plt
import time

# Will error if the minimal version of diffusers is not installed. Remove at your own risks.
check_min_version("0.17.0")

def get_feature_extractor(device):
    # Load a pre-trained VGG19 model
    vgg = models.vgg19(pretrained=True).features.to(device).eval()

    # Freeze all VGG parameters since we're only using it for feature extraction
    for param in vgg.parameters():
        param.requires_grad_(False)

    return vgg

def gram_matrix(tensor):
    # Get the batch_size, depth, height, and width of the Tensor
    _, d, h, w = tensor.size()
    # Reshape the Tensor so that the depth dimensions are flattened
    tensor = tensor.view(d, h * w)
    # Calculate the Gram matrix
    gram = torch.mm(tensor, tensor.t())
    return gram

def compute_style_loss(gen_features, style_grams, style_weights):
    style_loss = 0
    for layer in style_weights:
        # Get the generated image's features for this layer
        gen_feature = gen_features[layer]
        # Compute the Gram matrix for the generated image
        gen_gram = gram_matrix(gen_feature)
        # Get the style image's Gram matrix for this layer
        style_gram = style_grams[layer]
        # Calculate the style loss for this layer
        layer_style_loss = style_weights[layer] * torch.mean((gen_gram - style_gram)**2)
        # Add to the total style loss
        style_loss += layer_style_loss
    return style_loss


def import_model_class_from_model_name_or_path(pretrained_model_name_or_path: str, revision: str):
    text_encoder_config = PretrainedConfig.from_pretrained(
        pretrained_model_name_or_path,
        subfolder="text_encoder",
        revision=revision,
    )
    model_class = text_encoder_config.architectures[0]

    if model_class == "CLIPTextModel":
        from transformers import CLIPTextModel

        return CLIPTextModel
    elif model_class == "RobertaSeriesModelWithTransformation":
        from diffusers.pipelines.alt_diffusion.modeling_roberta_series import RobertaSeriesModelWithTransformation

        return RobertaSeriesModelWithTransformation
    elif model_class == "T5EncoderModel":
        from transformers import T5EncoderModel

        return T5EncoderModel
    else:
        raise ValueError(f"{model_class} is not supported.")

def tokenize_prompt(tokenizer, prompt, tokenizer_max_length=None):
    if tokenizer_max_length is not None:
        max_length = tokenizer_max_length
    else:
        max_length = tokenizer.model_max_length

    text_inputs = tokenizer(
        prompt,
        truncation=True,
        padding="max_length",
        max_length=max_length,
        return_tensors="pt",
    )

    return text_inputs

def encode_prompt(text_encoder, input_ids, attention_mask, text_encoder_use_attention_mask=False):
    text_input_ids = input_ids.to(text_encoder.device)

    if text_encoder_use_attention_mask:
        attention_mask = attention_mask.to(text_encoder.device)
    else:
        attention_mask = None

    prompt_embeds = text_encoder(
        text_input_ids,
        attention_mask=attention_mask,
    )
    prompt_embeds = prompt_embeds[0]

    return prompt_embeds

# model_path: path of the model
# image: input image, have not been pre-processed
# save_lora_dir: the path to save the lora
# prompt: the user input prompt
# lora_steps: number of lora training step
# lora_lr: learning rate of lora training
# lora_rank: the rank of lora
# def train_lora(image, prompt, save_lora_dir, model_path=None, tokenizer=None, text_encoder=None, vae=None, unet=None, noise_scheduler=None, lora_steps=200, lora_lr=2e-4, lora_rank=16, weight_name=None, safe_serialization=False, progress=tqdm):
def train_lora(image, prompt, save_lora_dir, model_path=None, tokenizer=None, text_encoder=None, vae=None, unet=None, noise_scheduler=None, lora_steps=200, lora_lr=2e-4, lora_rank=16, weight_name=None, safe_serialization=False, progress=tqdm, style_image=None, style_weights=None, style_weight=1e5): #, color_weight=1e5):

    # initialize accelerator
    accelerator = Accelerator(
        gradient_accumulation_steps=1,
        # mixed_precision='fp16'
    )
    set_seed(0)

    # Load the tokenizer
    if tokenizer is None:
        tokenizer = AutoTokenizer.from_pretrained(
            model_path,
            subfolder="tokenizer",
            revision=None,
            use_fast=False,
        )
    # initialize the model
    if noise_scheduler is None:
        noise_scheduler = DDPMScheduler.from_pretrained(model_path, subfolder="scheduler")
    if text_encoder is None:
        text_encoder_cls = import_model_class_from_model_name_or_path(model_path, revision=None)
        text_encoder = text_encoder_cls.from_pretrained(
            model_path, subfolder="text_encoder", revision=None
        )
    if vae is None:
        vae = AutoencoderKL.from_pretrained(
            model_path, subfolder="vae", revision=None
        )
    if unet is None:
        unet = UNet2DConditionModel.from_pretrained(
            model_path, subfolder="unet", revision=None
        )

    # set device and dtype
    device = torch.device("mps")#("cuda") if torch.cuda.is_available() else torch.device("cpu")

    vae.requires_grad_(False)
    text_encoder.requires_grad_(False)
    unet.requires_grad_(False)

    unet.to(device)
    vae.to(device)
    text_encoder.to(device)

    # initialize UNet LoRA
    unet_lora_attn_procs = {}
    for name, attn_processor in unet.attn_processors.items():
        cross_attention_dim = None if name.endswith("attn1.processor") else unet.config.cross_attention_dim
        if name.startswith("mid_block"):
            hidden_size = unet.config.block_out_channels[-1]
        elif name.startswith("up_blocks"):
            block_id = int(name[len("up_blocks.")])
            hidden_size = list(reversed(unet.config.block_out_channels))[block_id]
        elif name.startswith("down_blocks"):
            block_id = int(name[len("down_blocks.")])
            hidden_size = unet.config.block_out_channels[block_id]
        else:
            raise NotImplementedError("name must start with up_blocks, mid_blocks, or down_blocks")

        if isinstance(attn_processor, (AttnAddedKVProcessor, SlicedAttnAddedKVProcessor, AttnAddedKVProcessor2_0)):
            lora_attn_processor_class = LoRAAttnAddedKVProcessor
        else:
            lora_attn_processor_class = (
                LoRAAttnProcessor2_0 if hasattr(F, "scaled_dot_product_attention") else LoRAAttnProcessor
            )
        unet_lora_attn_procs[name] = lora_attn_processor_class(
            hidden_size=hidden_size, 
            cross_attention_dim=cross_attention_dim, rank=lora_rank
        )
    unet.set_attn_processor(unet_lora_attn_procs)
    unet_lora_layers = AttnProcsLayers(unet.attn_processors)

    # Optimizer creation
    params_to_optimize = (unet_lora_layers.parameters())
    optimizer = torch.optim.AdamW(
        params_to_optimize,
        lr=lora_lr,
        betas=(0.9, 0.999),
        weight_decay=1e-2,
        eps=1e-08,
    )

    lr_scheduler = get_scheduler(
        "constant",
        optimizer=optimizer,
        num_warmup_steps=0,
        num_training_steps=lora_steps,
        num_cycles=1,
        power=1.0,
    )

    # prepare accelerator
    unet_lora_layers = accelerator.prepare_model(unet_lora_layers)
    optimizer = accelerator.prepare_optimizer(optimizer)
    lr_scheduler = accelerator.prepare_scheduler(lr_scheduler)

    # initialize text embeddings
    with torch.no_grad():
        text_inputs = tokenize_prompt(tokenizer, prompt, tokenizer_max_length=None)
        text_embedding = encode_prompt(
            text_encoder,
            text_inputs.input_ids,
            text_inputs.attention_mask,
            text_encoder_use_attention_mask=False
        )

    if type(image) == np.ndarray:
        image = Image.fromarray(image)
        
    # initialize latent distribution
    image_transforms = transforms.Compose(
        [
            transforms.Resize(512, interpolation=transforms.InterpolationMode.BILINEAR),
            # transforms.RandomCrop(512),
            transforms.ToTensor(),
            transforms.Normalize([0.5], [0.5]),
        ]
    )

    image = image_transforms(image).to(device)
    image = image.unsqueeze(dim=0)

    # Load the feature extractor
    vgg = get_feature_extractor(device)

    # Define the layers to use for style representation
    if style_weights is None:
        style_weights = {
            '0': 1.0,  # conv1_1
            '5': 0.75, # conv2_1
            '10': 0.2, # conv3_1
            '19': 0.2, # conv4_1
            '28': 0.2  # conv5_1
        }

    # Preprocess the style image
    style_transform = T.Compose([
        T.Resize((512, 512)),
        T.ToTensor(),
    ])
    style_image = style_transform(style_image).to(device).unsqueeze(0)

    # Extract style features
    style_features = {}
    x = style_image
    for name, layer in vgg._modules.items():
        x = layer(x)
        if name in style_weights:
            style_features[name] = x
    # Compute Gram matrices for style features
    style_grams = {layer: gram_matrix(style_features[layer]) for layer in style_features}

    
    latents_dist = vae.encode(image).latent_dist

    loss_values = []
    time_values = []
    cumulative_time = 0
    cumulative_time_values = []

    for _ in progress.tqdm(range(lora_steps), desc="Training LoRA..."):
        start_time = time.time()

        unet.train()
        model_input = latents_dist.sample() * vae.config.scaling_factor
        # ... existing code to add noise and compute model_pred ...

        noise = torch.randn_like(model_input)
        bsz, channels, height, width = model_input.shape
        # Sample a random timestep for each image
        timesteps = torch.randint(
            0, noise_scheduler.config.num_train_timesteps, (bsz,), device=model_input.device
        )
        timesteps = timesteps.long()

        # Add noise to the model input according to the noise magnitude at each timestep
        # (this is the forward diffusion process)
        noisy_model_input = noise_scheduler.add_noise(model_input, noise, timesteps)

        # Predict the noise residual
        model_pred = unet(noisy_model_input, timesteps, text_embedding).sample

        if noise_scheduler.config.prediction_type == "epsilon":
            target = noise
        elif noise_scheduler.config.prediction_type == "v_prediction":
            target = noise_scheduler.get_velocity(model_input, noise, timesteps)
        else:
            raise ValueError(f"Unknown prediction type {noise_scheduler.config.prediction_type}")

        # Denoising loss
        loss = F.mse_loss(model_pred.float(), target.float(), reduction="mean")

        # Generate the image from the current latents
        with torch.no_grad():
            latents = model_input / vae.config.scaling_factor
            recon_images = vae.decode(latents).sample

        # Preprocess generated images for VGG
        gen_image = F.interpolate(recon_images, size=(512, 512), mode='bilinear', align_corners=False)

        # Extract features from generated images
        gen_features = {}
        x = gen_image
        for name, layer in vgg._modules.items():
            x = layer(x)
            if name in style_weights:
                gen_features[name] = x

        # Compute style loss
        style_loss = compute_style_loss(gen_features, style_grams, style_weights)

        # Total loss
        total_loss = loss + style_weight * style_loss

        accelerator.backward(total_loss)
        optimizer.step()
        lr_scheduler.step()
        optimizer.zero_grad()

        loss_values.append(total_loss.item())

        end_time = time.time()  # **6. Record end time**
        elapsed_time = end_time - start_time
        time_values.append(elapsed_time)

        cumulative_time += elapsed_time
        cumulative_time_values.append(cumulative_time)

    # for _ in progress.tqdm(range(lora_steps), desc="Training LoRA..."):
    #     unet.train()
    #     model_input = latents_dist.sample() * vae.config.scaling_factor
    #     # Sample noise that we'll add to the latents
    #     noise = torch.randn_like(model_input)
    #     bsz, channels, height, width = model_input.shape
    #     # Sample a random timestep for each image
    #     timesteps = torch.randint(
    #         0, noise_scheduler.config.num_train_timesteps, (bsz,), device=model_input.device
    #     )
    #     timesteps = timesteps.long()

    #     # Add noise to the model input according to the noise magnitude at each timestep
    #     # (this is the forward diffusion process)
    #     noisy_model_input = noise_scheduler.add_noise(model_input, noise, timesteps)

    #     # Predict the noise residual
    #     model_pred = unet(noisy_model_input, timesteps, text_embedding).sample

    #     # Get the target for loss depending on the prediction type
    #     if noise_scheduler.config.prediction_type == "epsilon":
    #         target = noise
    #     elif noise_scheduler.config.prediction_type == "v_prediction":
    #         target = noise_scheduler.get_velocity(model_input, noise, timesteps)
    #     else:
    #         raise ValueError(f"Unknown prediction type {noise_scheduler.config.prediction_type}")

    #     loss = F.mse_loss(model_pred.float(), target.float(), reduction="mean")
    #     accelerator.backward(loss)
    #     optimizer.step()
    #     lr_scheduler.step()
    #     optimizer.zero_grad()

    # save the trained lora
    # unet = unet.to(torch.float32)
    # vae = vae.to(torch.float32)
    # text_encoder = text_encoder.to(torch.float32)

    # unwrap_model is used to remove all special modules added when doing distributed training
    # so here, there is no need to call unwrap_model
    # unet_lora_layers = accelerator.unwrap_model(unet_lora_layers)
    LoraLoaderMixin.save_lora_weights(
        save_directory=save_lora_dir,
        unet_lora_layers=unet_lora_layers,
        text_encoder_lora_layers=None,
        weight_name=weight_name,
        safe_serialization=safe_serialization
    )  

    plt.figure(figsize=(10, 5))
    plt.plot(loss_values, label='Training Loss')
    plt.xlabel('Iterations')
    plt.ylabel('Loss')
    plt.title('Training Loss over Iterations')
    plt.legend()
    # **5. Save the plot to the save_lora_dir**
    plt.savefig(os.path.join(save_lora_dir, 'training_loss.png'))
    plt.close()

    plt.figure(figsize=(10, 5))
    plt.plot(time_values, label='Time per Iteration')
    plt.xlabel('Iterations')
    plt.ylabel('Time (s)')
    plt.title('Time Taken per Iteration')
    plt.legend()
    # **10. Save the time plot to the save_lora_dir**
    plt.savefig(os.path.join(save_lora_dir, 'training_time.png'))
    plt.close()

    # Plotting cumulative time taken
    plt.figure(figsize=(10, 5))
    plt.plot(cumulative_time_values, label='Cumulative Time')
    plt.xlabel('Iterations')
    plt.ylabel('Cumulative Time (s)')
    plt.title('Cumulative Time over Iterations')
    plt.legend()
    plt.savefig(os.path.join(save_lora_dir, 'cumulative_time.png'))
    plt.close()
    
def load_lora(unet, lora_0, lora_1, alpha):
    lora = {}
    for key in lora_0:
        lora[key] = (1 - alpha) * lora_0[key] + alpha * lora_1[key]
    unet.load_attn_procs(lora)
    return unet


def main(args):
    image = Image.open(args.image_path).convert("RGB")
    style_image = Image.open(args.style_image_path).convert('RGB')
    lora_steps = 200
    lora_lr = 2e-4
    lora_rank = 16
    style_weight = 1e4

    if not os.path.exists(args.save_lora_dir): os.mkdir(args.save_lora_dir)
    weight_name = 'lora_' + os.path.splitext(os.path.basename(args.image_path))[0] + '.ckpt'

    train_lora(image, args.prompt, args.save_lora_dir, args.model_key, None, None,
               None, None, None, lora_steps, lora_lr, lora_rank, weight_name=weight_name,
               style_image=style_image, style_weights=None, style_weight=style_weight)
    return


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--image_path', type=str, default='data/girl_c1.jpg')
    parser.add_argument('--style_image_path', type=str, required=True, help='Path to the style image.')
    parser.add_argument('--prompt', type=str, default='cartoon image, woman')
    parser.add_argument('--model_key', type=str, default='stabilityai/stable-diffusion-2-1-base')
    parser.add_argument('--save_lora_dir', type=str, default='lora_models')
    args = parser.parse_args()
    main(args)
