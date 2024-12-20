import copy
import glob
import os
from pathlib import Path
import cv2
import torch
import torch.nn as nn
import torchvision.transforms as T
import argparse
from PIL import Image
import yaml
from tqdm import tqdm
from transformers import logging
from diffusers import DDIMScheduler, StableDiffusionPipeline

from pnp_utils_combine import *
from diffusers.loaders import LoraLoaderMixin

# suppress partial model loading warning
logging.set_verbosity_error()

class PNP(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.device = config["device"]
        sd_version = config["sd_version"]

        if sd_version == '2.1':
            model_key = "stabilityai/stable-diffusion-2-1-base"
        elif sd_version == '2.0':
            model_key = "stabilityai/stable-diffusion-2-base"
        elif sd_version == '1.5':
            model_key = "runwayml/stable-diffusion-v1-5"
        else:
            raise ValueError(f'Stable-diffusion version {sd_version} not supported.')

        # Create SD models
        print('Loading SD model')

        if self.device == 'mps':
            dtype = torch.float32
        else:
            dtype = torch.float16

        pipe = StableDiffusionPipeline.from_pretrained(model_key, torch_dtype=torch.float32).to(self.device) #.to("cuda")
        # pipe.enable_xformers_memory_efficient_attention()

        self.vae = pipe.vae
        self.tokenizer = pipe.tokenizer
        self.text_encoder = pipe.text_encoder
        self.unet = pipe.unet

        self.scheduler = DDIMScheduler.from_pretrained(model_key, subfolder="scheduler")
        print(self.device)
        self.scheduler.set_timesteps(config["n_timesteps"], device=self.device)
        print('SD model loaded')

        # load image
        self.image, self.eps = self.get_data()

        self.text_embeds = self.get_text_embeds(config["prompt"], config["negative_prompt"])
        # self.pnp_guidance_embeds = self.get_text_embeds("", "").chunk(2)[0]
        self.pnp_guidance_embeds = self.get_text_embeds("", "")[:1]  # Shape [1, ...]
        
        self.unet_lora_list = []

        self.load_lora_weights(config['lora_configs'])

    def load_lora_weights(self, lora_configs):
        self.lora_models = []
        for config in lora_configs:
            # Create a copy of the UNet model
            unet_lora = copy.deepcopy(self.unet)
            # Load the LoRA weights
            lora_state_dict = torch.load(config['weight_path'], map_location=self.device)
            # LoraLoaderMixin.load_lora_weights(unet_lora, lora_state_dict)
            unet_lora.load_attn_procs(lora_state_dict)

            # Load the mask
            mask = Image.open(config['mask_path']).convert('L')
            mask = mask.resize((64, 64), Image.BILINEAR)
            mask = torch.tensor(np.array(mask) / 255.0, dtype=torch.float32).to(self.device)
            mask = mask.unsqueeze(0).unsqueeze(0)
            # Get text embeddings for this style
            text_embeds = self.get_text_embeds(config['prompt'], self.config["negative_prompt"])
            # Store the model, mask, and text embeddings
            self.lora_models.append({'unet': unet_lora, 'mask': mask, 'text_embeds': text_embeds})

    @torch.no_grad()
    def get_text_embeds(self, prompt, negative_prompt, batch_size=1):
        # Tokenize text and get embeddings
        text_input = self.tokenizer(prompt, padding='max_length', max_length=self.tokenizer.model_max_length,
                                    truncation=True, return_tensors='pt')
        text_embeddings = self.text_encoder(text_input.input_ids.to(self.device))[0]

        # Do the same for unconditional embeddings
        uncond_input = self.tokenizer(negative_prompt, padding='max_length', max_length=self.tokenizer.model_max_length,
                                      return_tensors='pt')

        uncond_embeddings = self.text_encoder(uncond_input.input_ids.to(self.device))[0]

        # Cat for final embeddings
        # text_embeddings = torch.cat([uncond_embeddings] * batch_size + [text_embeddings] * batch_size)
        text_embeddings = torch.cat([uncond_embeddings, text_embeddings], dim=0)
        return text_embeddings
        

    @torch.no_grad()
    def decode_latent(self, latents):
        if self.device != 'mps':
            # Use autocast only for 'cuda' or 'cpu'
            with torch.autocast(device_type=self.device.type, dtype=torch.float16):
                latents = 1 / 0.18215 * latents
                imgs = self.vae.decode(latents).sample
                imgs = (imgs / 2 + 0.5).clamp(0, 1)
        else:
            # Use float32 without autocast for 'mps'
            latents = 1 / 0.18215 * latents
            imgs = self.vae.decode(latents).sample
            imgs = (imgs / 2 + 0.5).clamp(0, 1)
        return imgs
    # def decode_latent(self, latent):
    #     with torch.autocast(device_type=self.device.type, dtype=torch.float32):
    #         latent = 1 / 0.18215 * latent
    #         img = self.vae.decode(latent).sample
    #         img = (img / 2 + 0.5).clamp(0, 1)
    #     return img

    # @torch.autocast(device_type=self.device.type, dtype=torch.float32)
    def get_data(self):
        # load image
        image = Image.open(self.config["image_path"]).convert('RGB') 
        image = image.resize((512, 512), resample=Image.Resampling.LANCZOS)
        image = T.ToTensor()(image).to(self.device)
        # get noise
        latents_path = os.path.join(self.config["latents_path"], os.path.splitext(os.path.basename(self.config["image_path"]))[0], f'noisy_latents_{self.scheduler.timesteps[0]}.pt')
        noisy_latent = torch.load(latents_path).to(self.device)
        return image, noisy_latent

    # @torch.no_grad()
    # def denoise_step(self, x, t):
    #     # register the time step and features in pnp injection modules
    #     source_latents = load_source_latents_t(t, os.path.join(self.config["latents_path"], os.path.splitext(os.path.basename(self.config["image_path"]))[0]))
    #     #latent_model_input = torch.cat([source_latents] + ([x] * 2))

    #     register_time(self.unet, t.item())
    #     for unet_lora in self.lora_list:
    #         register_time(unet_lora, t.item())
        
    #     # compute text embeddings
    #     # text_embed_input = torch.cat([self.pnp_guidance_embeds, self.text_embeds], dim=0)
    #     text_embeds = torch.cat([self.pnp_guidance_embeds, self.text_embeds], dim=0)  # Shape [3, ...]

    #     # latent_model_input = torch.cat([x] * 2)
    #     latent_model_input = torch.cat([source_latents, x, x])

    #     # apply the denoising network
    #     #print(self.unet)
    #     # noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embed_input)['sample']
    #     noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=self.text_embeds)['sample']
        

    #     # for i in range(0,len(self.lora_text_embeds_list)):
    #     #     text_embed_input = torch.cat([self.pnp_guidance_embeds, self.lora_text_embeds_list[i]], dim=0)
    #     #     noise_pred_lora = self.lora_list[i](latent_model_input, t, encoder_hidden_states=text_embed_input)['sample']
    #     #     noise_pred[:,:,self.mask_list[i]] = noise_pred_lora[:,:,self.mask_list[i]]

    #     for lora_model in self.lora_models:
    #         unet_lora = lora_model['unet']
    #         mask = lora_model['mask']
    #         text_embeds = lora_model['text_embeds']
    #         # Compute noise prediction with the LoRA UNet
    #         noise_pred_lora = unet_lora(latent_model_input, t, encoder_hidden_states=text_embeds)['sample']
    #         # Blend the noise predictions based on the mask
    #         noise_pred = noise_pred * (1 - mask) + noise_pred_lora * mask

    #     # perform guidance
    #     _, noise_pred_uncond, noise_pred_cond = noise_pred.chunk(3) #2
    #     noise_pred = noise_pred_uncond + self.config["guidance_scale"] * (noise_pred_cond - noise_pred_uncond)

    #     # compute the denoising step with the reference model
    #     denoised_latent = self.scheduler.step(noise_pred, t, x)['prev_sample']
    #     return denoised_latent

    def denoise_step(self, x, t):
        # Load source latents (used in PnP modules)
        source_latents = load_source_latents_t(
            t,
            os.path.join(
                self.config["latents_path"],
                os.path.splitext(os.path.basename(self.config["image_path"]))[0]
            )
        ).to(self.device)

        # Ensure source_latents has correct batch size
        if source_latents.dim() == 3:
            source_latents = source_latents.unsqueeze(0)  # Add batch dimension

        # Register time and source_latents in PnP modules
        register_time(self.unet, t.item(), source_latents)
        for lora_model in self.lora_models:
            register_time(lora_model['unet'], t.item(), source_latents)

        # Prepare latent_model_input with batch size 3
        latent_model_input = torch.cat([source_latents, x, x], dim=0)  # Shape: [3, C, H, W]

        # Prepare text embeddings with batch size 3
        text_embeds = torch.cat([self.pnp_guidance_embeds, self.text_embeds], dim=0)  # Shape: [3, ...]

        # Apply the denoising network
        noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embeds)['sample']

        # Apply the LoRA models
        for lora_model in self.lora_models:
            unet_lora = lora_model['unet']
            mask = lora_model['mask']
            lora_text_embeds = torch.cat([self.pnp_guidance_embeds, lora_model['text_embeds']], dim=0)  # Shape: [3, ...]
            noise_pred_lora = unet_lora(latent_model_input, t, encoder_hidden_states=lora_text_embeds)['sample']
            # Blend the noise predictions based on the mask
            noise_pred = noise_pred * (1 - mask) + noise_pred_lora * mask

        # Perform guidance
        _, noise_pred_uncond, noise_pred_cond = noise_pred.chunk(3)
        noise_pred = noise_pred_uncond + self.config["guidance_scale"] * (noise_pred_cond - noise_pred_uncond)

        # Compute the denoising step with the scheduler
        denoised_latent = self.scheduler.step(noise_pred, t, x)['prev_sample']
        return denoised_latent

    @torch.no_grad()
    def denoise_step_all(self, x, t):
        # register the time step and features in pnp injection modules
        source_latents = load_source_latents_t(t, os.path.join(self.config["latents_path"], os.path.splitext(os.path.basename(self.config["image_path"]))[0]))
        latent_model_input = torch.cat([source_latents] + ([x] * 2))

        register_time(self.unet, t.item())
        for unet_lora in self.lora_list:
            register_time(unet_lora, t.item())

        # compute text embeddings
        text_embed_input = torch.cat([self.pnp_guidance_embeds, self.text_embeds], dim=0)

        # apply the denoising network
        #print(self.unet)
        noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embed_input)['sample']
        noise_pred_c = noise_pred.clone()

        for i in range(0,len(self.lora_text_embeds_list)):
            text_embed_input = torch.cat([self.pnp_guidance_embeds, self.lora_text_embeds_list[i]], dim=0)
            noise_pred_lora = self.lora_list[i](latent_model_input, t, encoder_hidden_states=text_embed_input)['sample']
            noise_pred[:,:,self.mask_list[i]] = noise_pred_lora[:,:,self.mask_list[i]]

        # perform guidance
        _, noise_pred_uncond, noise_pred_cond = noise_pred.chunk(3)
        noise_pred = noise_pred_uncond + self.config["guidance_scale"] * (noise_pred_cond - noise_pred_uncond)

        _, noise_pred_uncond, noise_pred_cond = noise_pred_c.chunk(3)
        noise_pred_c = noise_pred_uncond + self.config["guidance_scale"] * (noise_pred_cond - noise_pred_uncond)

        _, noise_pred_uncond, noise_pred_cond = noise_pred_lora.chunk(3)
        noise_pred_lora = noise_pred_uncond + self.config["guidance_scale"] * (noise_pred_cond - noise_pred_uncond)

        # compute the denoising step with the reference model
        denoised_latent = self.scheduler.step(noise_pred, t, x)['prev_sample']
        denoised_latent_c = self.scheduler.step(noise_pred_c, t, x)['prev_sample']
        denoised_latent_lora = self.scheduler.step(noise_pred_lora, t, x)['prev_sample']        
        return denoised_latent, denoised_latent_c, denoised_latent_lora
    

    def init_pnp(self, conv_injection_t, qk_injection_t):
        self.qk_injection_timesteps = self.scheduler.timesteps[:qk_injection_t] if qk_injection_t >= 0 else []
        self.conv_injection_timesteps = self.scheduler.timesteps[:conv_injection_t] if conv_injection_t >= 0 else []
        register_attention_control_efficient(self.unet, self.qk_injection_timesteps)
        register_conv_control_efficient(self.unet, self.conv_injection_timesteps)

    def run_pnp(self):
        pnp_f_t = int(self.config["n_timesteps"] * self.config["pnp_f_t"])
        pnp_attn_t = int(self.config["n_timesteps"] * self.config["pnp_attn_t"])
        pnp_f_t = 50
        pnp_attn_t = 50
        self.init_pnp(conv_injection_t=pnp_f_t, qk_injection_t=pnp_attn_t)
        edited_img = self.sample_loop(self.eps)

    def sample_loop(self, x):
        if self.device != 'mps':
            # Use autocast only for 'cuda' or 'cpu'
            with torch.autocast(device_type=self.device, dtype=torch.float16):
                for i, t in enumerate(tqdm(self.scheduler.timesteps, desc="Sampling")):
                    x = self.denoise_step(x, t)
                decoded_latent = self.decode_latent(x)
                T.ToPILImage()(decoded_latent[0]).save(f'{self.config["output_path"]}/output-{self.config["prompt"]}.png') 
        else:
            with torch.no_grad():
                for i, t in enumerate(tqdm(self.scheduler.timesteps, desc="Sampling")):
                    x = self.denoise_step(x, t)
                decoded_latent = self.decode_latent(x)
                T.ToPILImage()(decoded_latent[0]).save(f'{self.config["output_path"]}/output-{self.config["prompt"]}.png') 
        return decoded_latent
        # with torch.autocast(device_type='cuda', dtype=torch.float32):
        #     for i, t in enumerate(tqdm(self.scheduler.timesteps, desc="Sampling")):
        #         x = self.denoise_step(x, t)
        #     decoded_latent = self.decode_latent(x)
        #     T.ToPILImage()(decoded_latent[0]).save(f'{self.config["output_path"]}/output-{self.config["prompt"]}.png')
                
        # return decoded_latent
    
    def init_pnp_lora(self, unet_lora, conv_injection_t, qk_injection_t):
        self.qk_injection_timesteps = self.scheduler.timesteps[:qk_injection_t] if qk_injection_t >= 0 else []
        self.conv_injection_timesteps = self.scheduler.timesteps[:conv_injection_t] if conv_injection_t >= 0 else []
        register_attention_control_efficient(unet_lora, self.qk_injection_timesteps)
        register_conv_control_efficient(unet_lora, self.conv_injection_timesteps)    
    
    
    def load_lora(self):
        self.mask_list = []
        self.lora_list = []
        self.lora_text_embeds_list = []
        for lora_name in self.lora_name_list:
            unet_lora_temp = copy.deepcopy(self.unet)
            load_lora_paths = './lora_models/' + lora_name.strip() + '.ckpt'
            lora = torch.load(load_lora_paths, map_location="mps")
            unet_lora_temp.load_attn_procs(lora)
            self.init_pnp_lora(unet_lora_temp,30,25)
            self.lora_list.append(unet_lora_temp)
        for mask_name in self.mask_name_list:
            mask_path = 'mask/'+ mask_name.strip() + '.png'
            mask = cv2.imread(mask_path)
            mask = cv2.resize(mask,[64,64])
            mask = np.array(mask, dtype = bool)[:,:,0]
            self.mask_list.append(mask)
        for prompts in self.prompt_gene_list:
            text_embeds = self.get_text_embeds(prompts.strip(), config["negative_prompt"])
            self.lora_text_embeds_list.append(text_embeds)
        return


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config_path', type=str, default='configs/config-girl1.yaml')
    opt = parser.parse_args()
    with open(opt.config_path, "r") as f:
        config = yaml.safe_load(f)
    os.makedirs(config["output_path"], exist_ok=True)
    with open(os.path.join(config["output_path"], "config.yaml"), "w") as f:
        yaml.dump(config, f)
    
    seed_everything(config["seed"])
    print(config)
    pnp = PNP(config)
    pnp.prompt_gene_list = config['prompt_gene'].split(';')
    pnp.lora_name_list = config['lora_name'].split(';')
    pnp.mask_name_list = config['mask'].split(';')
    pnp.load_lora()
    pnp.run_pnp()