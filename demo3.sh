python lora_train.py --image_path data/deer_c1.jpg --prompt 'painting of <sss>, deer, grass' --style_image_path data/deer1.jpg

python preprocess.py --data_path data/deer1.jpg

python diffstyler.py --config_path configs/config-deer1-1.yaml