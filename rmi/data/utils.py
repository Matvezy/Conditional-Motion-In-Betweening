import json
import os

import numpy as np
import torch
from rmi.data.quaternion import euler_to_quaternion, qeuler_np


def generate_infogan_code(batch_size, sequence_length, discrete_code_dim, device):
    """Generate discrete infogan latent code to given input data"""

    if discrete_code_dim != 0:
        label_idx = torch.randint(low=0, high=discrete_code_dim, size=(batch_size * sequence_length,), device=device)
        discrete_code = torch.nn.functional.one_hot(label_idx, num_classes=discrete_code_dim)
    return discrete_code.reshape(batch_size, sequence_length, discrete_code_dim), label_idx.reshape(batch_size, sequence_length)


def append_infogan_code(input_data, discrete_code_dim, device):
    """Append discrete infogan latent code to given input data"""

    num_samples = input_data.shape[0]

    if discrete_code_dim != 0:
        label_idx = torch.randint(low=0, high=discrete_code_dim, size=(num_samples,), device=device)
        discrete_code = torch.nn.functional.one_hot(label_idx)
    return_vec = torch.cat([input_data, discrete_code], dim=1)
    return return_vec, label_idx


def write_json(filename, local_q, root_pos, joint_names):
    json_out = {}
    json_out['root_pos'] = root_pos.tolist()
    json_out['local_quat'] = local_q.tolist()
    json_out['joint_names'] = joint_names
    with open(filename, 'w') as outfile:
        json.dump(json_out, outfile)


def flip_bvh(bvh_folder: str):
    """
    Generate LR flip of existing bvh files. Assumes Z-forward.
    """

    print("Left-Right Flipping Process...")

    # List files which are not flipped yet
    to_convert = []
    bvh_files = os.listdir(bvh_folder)
    for bvh_file in bvh_files:
        if '_LRflip.bvh' in bvh_file:
            continue
        flipped_file = bvh_file.replace('.bvh', '_LRflip.bvh')
        if flipped_file in bvh_files:
            print(f"[SKIP: {bvh_file}] (flipped file already exists)")
            continue
        to_convert.append(bvh_file)
    
    print("Following files will be flipped: ")
    print(to_convert)

    for i, converting_fn in enumerate(to_convert):
        fout = open(os.path.join(bvh_folder, converting_fn.replace('.bvh', '_LRflip.bvh')), 'w')
        file_read = open(os.path.join(bvh_folder, converting_fn), 'r')
        file_lines = file_read.readlines()
        hierarchy_part = True
        for line in file_lines:
            if hierarchy_part:
                fout.write(line)
                if 'Frame Time' in line:  
                    # This should be the last exact copy. Motion line comes next
                    hierarchy_part = False
            else:
                # Followings are very helpful to understand which axis needs to be inverted
                # http://lo-th.github.io/olympe/BVH_player.html
                # https://quaternions.online/
                str_to_num = line.split(' ')[:-1]  # Extract number only
                motion_mat = np.array([float(x) for x in str_to_num]).reshape(23, 3) # Hips 6 Channel + 3 * 21 = 69
                motion_mat[0,2] *= -1.0  # Invert translation Z axis (forward-backward)
                quat = euler_to_quaternion(np.radians(motion_mat[1:]), 'zyx')  # This function takes radians
                # Invert X-axis (Left-Right) / Quaternion representation: (w, x, y, z)
                quat[:,0] *= -1.0
                quat[:,1] *= -1.0 
                motion_mat[1:] = np.degrees(qeuler_np(quat, 'zyx'))
                
                # idx 0: Hips Wolrd coord, idx 1: Hips Rotation
                left_idx = [2,3,4,5,15,16,17,18]  # From 2: LeftUpLeg...
                right_idx = [6,7,8,9,19,20,21,22]  # From 6: RightUpLeg...
                motion_mat[left_idx+right_idx] = motion_mat[right_idx+left_idx].copy()
                motion_mat = np.round(motion_mat, decimals=6)
                motion_vector = np.reshape(motion_mat, (69,))
                motion_part_str = ''
                for s in motion_vector:
                    motion_part_str += (str(s) + ' ')
                motion_part_str += '\n'
                fout.write(motion_part_str)
        print(f"[{i+1}/{len(to_convert)}] {converting_fn} flipped.")

