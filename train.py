import os
import pathlib
import shutil
from datetime import datetime

import numpy as np
import torch
import torch.nn as nn
import yaml
from kpt.model.skeleton import TorchSkeleton
from pymo.parsers import BVHParser
from torch.optim import Adam
from torch.utils.data import DataLoader
from torch.utils.tensorboard.writer import SummaryWriter
from tqdm import tqdm

from rmi.data.lafan1_dataset import LAFAN1Dataset
from rmi.data.utils import flip_bvh, generate_infogan_code
from rmi.model.network import (Decoder, InfoGANDiscriminator, InputEncoder,
                               LSTMNetwork, SinglePoseDiscriminator)
from rmi.model.noise_injector import noise_injector
from rmi.model.positional_encoding import PositionalEncoding


def train():
    # Load configuration from yaml
    config = yaml.safe_load(open('./config/config_base.yaml', 'r').read())

    # Set device to use
    # TODO: Support Multi GPU
    gpu_id = config['device']['gpu_id']
    device = torch.device(f"cuda:{gpu_id}" if torch.cuda.is_available() else "cpu")

    # Set number of InfoGAN Code
    infogan_code = config['model']['infogan_code']

    # Prepare Directory
    time_stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    model_path = os.path.join('model_weights', time_stamp)
    pathlib.Path(model_path).mkdir(parents=True, exist_ok=True)
    shutil.copyfile('./config/config_base.yaml', os.path.join(model_path, 'exp_config.yaml'))

    # Prepare Tensorboard
    tb_path = os.path.join('tensorboard', time_stamp)
    pathlib.Path(tb_path).mkdir(parents=True, exist_ok=True)
    summarywriter = SummaryWriter(log_dir=tb_path)
    
    # Load Skeleton
    parsed = BVHParser().parse(config['data']['skeleton_path']) # Use first bvh info as a reference skeleton.
    skeleton = TorchSkeleton(skeleton=parsed.skeleton, root_name='Hips', device=device)

    # Flip, Load and preprocess data. It utilizes LAFAN1 utilities
    flip_bvh(config['data']['data_dir'])

    # Load LAFAN Dataset
    lafan_dataset = LAFAN1Dataset(lafan_path=config['data']['data_dir'], train=True, device=device, start_seq_length=30, cur_seq_length=30, max_transition_length=30)
    lafan_data_loader = DataLoader(lafan_dataset, batch_size=config['model']['batch_size'], shuffle=True, num_workers=config['data']['data_loader_workers'])

    # Extract dimension from processed data
    root_v_dim = lafan_dataset.root_v_dim
    local_q_dim = lafan_dataset.local_q_dim
    contact_dim = lafan_dataset.contact_dim
    ig_d_code_dim = infogan_code

    # Initializing networks
    state_in = root_v_dim + local_q_dim + contact_dim
    infogan_in = state_in + ig_d_code_dim
    state_encoder = InputEncoder(input_dim=infogan_in)
    state_encoder.to(device)

    offset_in = root_v_dim + local_q_dim
    offset_encoder = InputEncoder(input_dim=offset_in)
    offset_encoder.to(device)

    target_in = local_q_dim
    target_encoder = InputEncoder(input_dim=target_in)
    target_encoder.to(device)

    # LSTM
    lstm_in = state_encoder.out_dim * 3
    lstm_hidden = config['model']['lstm_hidden']
    lstm = LSTMNetwork(input_dim=lstm_in, hidden_dim=lstm_hidden, device=device)
    lstm.to(device)

    # Decoder
    decoder = Decoder(input_dim=lstm_hidden, out_dim=state_in)
    decoder.to(device)

    discriminator_in = lafan_dataset.num_joints * 3 * 2 # See Appendix
    sp_discriminator_in = discriminator_in + local_q_dim
    single_pose_discriminator = SinglePoseDiscriminator(input_dim=sp_discriminator_in, discrete_code_dim=infogan_code)
    single_pose_discriminator.to(device)
    short_discriminator = InfoGANDiscriminator(input_dim=discriminator_in, discrete_code_dim=infogan_code, length=2)
    short_discriminator.to(device)
    long_discriminator = InfoGANDiscriminator(input_dim=discriminator_in, discrete_code_dim=infogan_code, length=5)
    long_discriminator.to(device)

    infogan_disc_loss = nn.CrossEntropyLoss()

    pe = PositionalEncoding(dimension=256, max_len=lafan_dataset.max_transition_length, device=device)

    generator_optimizer = Adam(params=list(state_encoder.parameters()) + 
                                      list(offset_encoder.parameters()) + 
                                      list(target_encoder.parameters()) +
                                      list(lstm.parameters()) +
                                      list(decoder.parameters()),
                                lr=config['model']['learning_rate'],
                                betas=(config['model']['optim_beta1'], config['model']['optim_beta2']),
                                amsgrad=True)

    discriminator_optimizer = Adam(params=list(short_discriminator.parameters()) +
                                          list(long_discriminator.parameters()),
                                    lr=config['model']['learning_rate'],
                                    betas=(config['model']['optim_beta1'], config['model']['optim_beta2']),
                                    amsgrad=True)

    pdist = nn.PairwiseDistance(p=2)

    for epoch in tqdm(range(config['model']['epochs']), position=0, desc="Epoch"):

        # Control transition length
        if lafan_dataset.cur_seq_length < lafan_dataset.max_transition_length:
            lafan_dataset.cur_seq_length =  np.int32(1/lafan_dataset.increase_rate * epoch + lafan_dataset.start_seq_length)

        state_encoder.train()
        offset_encoder.train()
        target_encoder.train()
        lstm.train()
        decoder.train()

        batch_pbar = tqdm(lafan_data_loader, position=1, desc="Batch")
        for sampled_batch in batch_pbar:
            loss_pos = 0
            loss_quat = 0
            loss_contact = 0
            loss_root = 0
            div_adv = 0

            current_batch_size = len(sampled_batch['global_pos'])

            # state input
            local_q = sampled_batch['local_q'].to(device)
            root_v = sampled_batch['root_v'].to(device)
            contact = sampled_batch['contact'].to(device)
            # offset input
            root_p_offset = sampled_batch['root_p_offset'].to(device)
            local_q_offset = sampled_batch['local_q_offset'].to(device)
            local_q_offset = local_q_offset.view(current_batch_size, -1)
            # target input
            target = sampled_batch['q_target'].to(device)
            target = target.view(current_batch_size, -1)
            # root pos
            root_p = sampled_batch['root_p'].to(device)
            # global pos
            global_pos = sampled_batch['global_pos'].to(device)

            lstm.init_hidden(current_batch_size)
            pred_list = []
            pred_list.append(global_pos[:,0])

            # 3.4: target noise is sampled once per sequence
            target_noise = torch.normal(mean=0, std=config['model']['target_noise'], size=root_v.shape, device=device)

            # Generating Frames
            training_frames = torch.randint(low=lafan_dataset.start_seq_length, high=lafan_dataset.cur_seq_length + 1, size=(1,))[0]

            # Generate Infogan Code (batch, length, disc_code)
            infogan_code_gen, fake_indices = generate_infogan_code(batch_size=current_batch_size, sequence_length=training_frames, discrete_code_dim=ig_d_code_dim, device=device)
            
            ## EXP
            diverging_code_0 = torch.zeros_like(infogan_code_gen, device=device)
            diverging_code_0[:, :, 0] = 1
            diverging_code_1 = torch.zeros_like(infogan_code_gen, device=device)
            diverging_code_1[:, :, 1] = 1

            local_q_pred_list = []
            for t in range(training_frames):
                if t  == 0: # if initial frame
                    root_p_t = root_p[:,t]
                    root_v_t = root_v[:,t]

                    local_q_t = local_q[:,t]
                    local_q_t = local_q_t.view(local_q_t.size(0), -1)
                    contact_t = contact[:,t]
                else:
                    root_p_t = root_pred  # Be careful about dimension
                    root_v_t = root_v_pred[0]

                    local_q_t = local_q_pred[0]
                    contact_t = contact_pred[0]

                assert root_p_offset.shape == root_p_t.shape

                # state input
                vanilla_state_input = torch.cat([local_q_t, root_v_t, contact_t], -1)

                # concatenate InfoGAN code
                state_input = torch.cat([vanilla_state_input, infogan_code_gen[:, t]], dim=1)

                ## EXP
                diverging_state_0 = torch.cat([vanilla_state_input, diverging_code_0[:, t]], dim=1)
                diverging_state_1 = torch.cat([vanilla_state_input, diverging_code_1[:, t]], dim=1)

                # offset input
                root_p_offset_t = root_p_offset - root_p_t
                local_q_offset_t = local_q_offset - local_q_t
                offset_input = torch.cat([root_p_offset_t, local_q_offset_t], -1)
                # target input
                target_input = target

                h_state = state_encoder(state_input)

                # EXP
                h_state_diverging_0 = state_encoder(diverging_state_0)
                h_state_diverging_1 = state_encoder(diverging_state_1)

                h_offset = offset_encoder(offset_input)
                h_target = target_encoder(target_input)
                
                # Use positional encoding
                tta = training_frames - t
                h_state = pe(h_state, tta)

                # EXP
                h_state_diverging_0 = pe(h_state_diverging_0, tta)
                h_state_diverging_1 = pe(h_state_diverging_1, tta)

                h_offset = pe(h_offset, tta)  # (batch size, 256)
                h_target = pe(h_target, tta)  # (batch size, 256)

                offset_target = torch.cat([h_offset, h_target], dim=1)

                # lstm
                h_in = torch.cat([h_state, offset_target], dim=1).unsqueeze(0)

                # EXP
                h_div_0_in = torch.cat([h_state_diverging_0, offset_target], dim=1).unsqueeze(0)
                h_div_1_in = torch.cat([h_state_diverging_1, offset_target], dim=1).unsqueeze(0)

                h_out = lstm(h_in)
                
                # EXP
                h_div_0_out = lstm(h_div_0_in)
                h_div_1_out = lstm(h_div_1_in)

                # decoder
                h_pred, contact_pred = decoder(h_out)
                local_q_v_pred = h_pred[:,:,:target_in]
                local_q_pred = local_q_v_pred + local_q_t
                local_q_pred_list.append(local_q_pred[0])

                local_q_pred_ = local_q_pred.view(local_q_pred.size(0), local_q_pred.size(1), -1, 4)
                local_q_pred_ = local_q_pred_ / torch.norm(local_q_pred_, dim = -1, keepdim = True)

                root_v_pred = h_pred[:,:,target_in:]
                root_pred = root_v_pred + root_p_t

                # EXP
                div_0_h_pred, div_0_contact_pred = decoder(h_div_0_out)
                div_0_local_q_v_pred = div_0_h_pred[:,:,:target_in]
                div_0_local_q_pred = div_0_local_q_v_pred + local_q_t
                div_0_local_q_pred_ = div_0_local_q_pred.view(div_0_local_q_pred.size(0), div_0_local_q_pred.size(1), -1, 4)
                div_0_local_q_pred_ = div_0_local_q_pred_ / torch.norm(div_0_local_q_pred_, dim = -1, keepdim = True)
                div_0_root_v_pred = div_0_h_pred[:,:,target_in:]
                div_0_root_pred = div_0_root_v_pred + root_p_t
                
                div_1_h_pred, div_1_contact_pred = decoder(h_div_1_out)
                div_1_local_q_v_pred = div_1_h_pred[:,:,:target_in]
                div_1_local_q_pred = div_1_local_q_v_pred + local_q_t
                div_1_local_q_pred_ = div_1_local_q_pred.view(div_1_local_q_pred.size(0), div_1_local_q_pred.size(1), -1, 4)
                div_1_local_q_pred_ = div_1_local_q_pred_ / torch.norm(div_1_local_q_pred_, dim = -1, keepdim = True)
                div_1_root_v_pred = div_1_h_pred[:,:,target_in:]
                div_1_root_pred = div_1_root_v_pred + root_p_t

                # FK
                root_pred = root_pred.squeeze()
                local_q_pred_ = local_q_pred_.squeeze()
                pos_pred, _ = skeleton.forward_kinematics(root_pred, local_q_pred_, rot_repr='quaternion')
                pred_list.append(pos_pred)

                # EXP
                div_0_root_pred = div_0_root_pred.squeeze()
                div_0_local_q_pred_ = div_0_local_q_pred_.squeeze()

                div_1_root_pred = div_1_root_pred.squeeze()
                div_1_local_q_pred_ = div_1_local_q_pred_.squeeze()

                # Loss
                pos_next = global_pos[:,t+1]
                local_q_next = local_q[:,t+1]
                local_q_next = local_q_next.view(local_q_next.size(0), -1)
                root_p_next = root_p[:,t+1]
                contact_next = contact[:,t+1]
                # EXP
                noise_multiplier = noise_injector(t, length=training_frames)  # Noise injection
                div_adv += torch.mean(pdist(div_0_root_pred, div_1_root_pred) * noise_multiplier)

                # Calculate L1 Norm
                # 3.7.3: We scale all of our losses to be approximately equal on the LaFAN1 dataset 
                # for an untrained network before tuning them with custom weights.
                # loss_pos += torch.mean(torch.abs(pos_pred - pos_next)) / training_frames
                # loss_root += torch.mean(torch.abs(root_pred - root_p_next)) / training_frames
                # loss_quat += torch.mean(torch.abs(local_q_pred[0] - local_q_next)) / training_frames
                # loss_contact += torch.mean(torch.abs(contact_pred[0] - contact_next)) / training_frames

            # Adversarial
            fake_pos_input = torch.cat([x.reshape(current_batch_size, -1).unsqueeze(-1) for x in pred_list[:-1]], -1)
            fake_v_input = torch.cat([fake_pos_input[:,:,1:] - fake_pos_input[:,:,:-1], torch.zeros_like(fake_pos_input[:,:,0:1], device=device)], -1)
            fake_input = torch.cat([fake_pos_input, fake_v_input], 1)

            real_pos_input = torch.cat([global_pos[:, i].reshape(current_batch_size, -1).unsqueeze(-1) for i in range(lafan_dataset.cur_seq_length)], -1)
            real_v_input = torch.cat([real_pos_input[:,:,1:] - real_pos_input[:,:,:-1], torch.zeros_like(real_pos_input[:,:,0:1], device=device)], -1)
            real_input = torch.cat([real_pos_input, real_v_input], 1)

            assert fake_input.shape == real_input.shape

            single_pose_pred_quaternion = torch.stack(local_q_pred_list, -1)
            single_pose_real_quaternion = local_q[:,:lafan_dataset.cur_seq_length].reshape(current_batch_size, lafan_dataset.cur_seq_length, -1).permute(0,2,1)

            assert single_pose_pred_quaternion.shape == single_pose_real_quaternion.shape

            single_pose_fake_input = torch.cat([fake_input, single_pose_pred_quaternion], dim=1)
            single_pose_real_input = torch.cat([real_input, single_pose_real_quaternion], dim=1)

            ## Discriminator
            discriminator_optimizer.zero_grad()

            # InfoGAN Loss (maintain LSGAN for original gal V(D,G))
            
            ## Single pose discriminator
            sp_fake_input = single_pose_fake_input.permute(0,2,1).reshape(-1, sp_discriminator_in).detach()
            sp_d_fake_gan_out, _ = single_pose_discriminator(sp_fake_input.detach())
            sp_d_fake_gan_score = sp_d_fake_gan_out[:, 0]

            sp_real_input = single_pose_real_input.permute(0,2,1).reshape(-1, sp_discriminator_in).detach()
            sp_d_real_gan_out, _ = single_pose_discriminator(sp_real_input)
            sp_d_real_gan_score = sp_d_real_gan_out[:, 0]

            sp_d_fake_loss = torch.mean((sp_d_fake_gan_score) ** 2)
            sp_d_real_loss = torch.mean((sp_d_real_gan_score - 1) ** 2)
            sp_d_loss = (sp_d_fake_loss + sp_d_real_loss) / 2.0

            ## Short discriminator
            short_d_fake_gan_out, _ = short_discriminator(fake_input.detach())
            short_d_fake_gan_score = torch.mean(short_d_fake_gan_out[:,0], dim=1)

            short_d_real_gan_out, _ = short_discriminator(real_input)
            short_d_real_gan_score = torch.mean(short_d_real_gan_out[:,0], dim=1)

            short_d_fake_loss = torch.mean((short_d_fake_gan_score) ** 2)  
            short_d_real_loss = torch.mean((short_d_real_gan_score -  1) ** 2)

            short_d_loss = (short_d_fake_loss + short_d_real_loss) / 2.0

            ## Long  discriminator
            long_d_fake_gan_out, _ = long_discriminator(fake_input.detach())
            long_d_fake_gan_score = torch.mean(long_d_fake_gan_out[:,0], dim=1)

            long_d_real_gan_out, _ = long_discriminator(real_input)
            long_d_real_gan_score = torch.mean(long_d_real_gan_out[:,0], dim=1)

            long_d_fake_loss = torch.mean((long_d_fake_gan_score) ** 2)
            long_d_real_loss = torch.mean((long_d_real_gan_score -  1) ** 2)

            long_d_loss = (long_d_fake_loss + long_d_real_loss) / 2.0

            total_d_loss = config['model']['loss_sp_discriminator_weight'] * (sp_d_loss) + \
                            config['model']['loss_discriminator_weight'] * (short_d_loss + long_d_loss)
            total_d_loss.backward()
            discriminator_optimizer.step()

            generator_optimizer.zero_grad()

            # l1_loss = config['model']['loss_pos_weight'] * loss_pos + \
            #           config['model']['loss_quat_weight'] * loss_quat + \
            #           config['model']['loss_root_weight'] * loss_root + \
            #           config['model']['loss_contact_weight'] * loss_contact
            
            # Adversarial
            ## Single pose generator
            sp_g_fake_gan_out, sp_g_fake_q_discrete = single_pose_discriminator(sp_fake_input)
            sp_g_fake_gan_score = sp_g_fake_gan_out[:, 0]
            sp_g_fake_loss = torch.mean((sp_g_fake_gan_score - 1) ** 2)
            sp_disc_code_loss = infogan_disc_loss(sp_g_fake_q_discrete, fake_indices.reshape(sp_g_fake_q_discrete.shape[0]))

            short_g_fake_gan_out, _ = short_discriminator(fake_input)
            short_g_score = torch.mean(short_g_fake_gan_out[:,0], dim=1)
            short_g_loss = torch.mean((short_g_score -  1) ** 2)

            long_g_fake_gan_out, _ = long_discriminator(fake_input)
            long_g_score = torch.mean(long_g_fake_gan_out[:,0], dim=1)
            long_g_loss = torch.mean((long_g_score -  1) ** 2)

            total_g_loss = config['model']['loss_sp_generator_weight'] * (sp_disc_code_loss + sp_g_fake_loss) + \
                           config['model']['loss_generator_weight'] * (short_g_loss + long_g_loss)
        
            loss_total = total_g_loss - div_adv * config['model']['divergence_weight']

            # TOTAL LOSS
            loss_total.backward()

            # Gradient clipping for training stability
            torch.nn.utils.clip_grad_norm_(state_encoder.parameters(), 1.0)
            torch.nn.utils.clip_grad_norm_(offset_encoder.parameters(), 1.0)
            torch.nn.utils.clip_grad_norm_(target_encoder.parameters(), 1.0)
            torch.nn.utils.clip_grad_norm_(lstm.parameters(), 1.0)
            torch.nn.utils.clip_grad_norm_(decoder.parameters(), 1.0)
            generator_optimizer.step()
            batch_pbar.set_postfix({'LOSS': np.round(loss_total.item(), decimals=3)})


        summarywriter.add_scalar("LOSS/Generator", total_g_loss, epoch + 1)
        summarywriter.add_scalar("LOSS/Discriminator", total_d_loss, epoch + 1)
        summarywriter.add_scalar("LOSS/Divergence Advantage", div_adv, epoch + 1)
        summarywriter.add_scalar("LOSS/Total Loss (L1 + Generator)", loss_total, epoch + 1)

        if (epoch + 1) % config['log']['weight_save_interval'] == 0:
            weight_epoch = 'trained_weight_' + str(epoch + 1)
            weight_path = os.path.join(model_path, weight_epoch)
            pathlib.Path(weight_path).mkdir(parents=True, exist_ok=True)
            torch.save(state_encoder.state_dict(), weight_path + '/state_encoder.pkl')
            torch.save(target_encoder.state_dict(), weight_path + '/target_encoder.pkl')
            torch.save(offset_encoder.state_dict(), weight_path + '/offset_encoder.pkl')
            torch.save(lstm.state_dict(), weight_path + '/lstm.pkl')
            torch.save(decoder.state_dict(), weight_path + '/decoder.pkl')
            torch.save(short_discriminator.state_dict(), weight_path + '/short_discriminator.pkl')
            torch.save(long_discriminator.state_dict(), weight_path + '/long_discriminator.pkl')
            if config['model']['save_optimizer']:
                torch.save(generator_optimizer.state_dict(), weight_path + '/generator_optimizer.pkl')
                torch.save(discriminator_optimizer.state_dict(), weight_path + '/discriminator_optimizer.pkl')


if __name__ == '__main__':
    train()
