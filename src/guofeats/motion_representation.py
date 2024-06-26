# Taken and adapated from
# https://github.com/EricGuo5513/HumanML3D/blob/main/motion_representation.ipynb
from .common.skeleton import Skeleton
import numpy as np
import os
from .common.quaternion import (
    qrot,
    qbetween_np,
    qrot_np,
    qfix,
    qmul_np,
    qinv_np,
    qinv,
    quaternion_to_cont6d_np,
    quaternion_to_cont6d,
)

from .paramUtil import t2m_raw_offsets, t2m_kinematic_chain

import torch


def uniform_skeleton(
    positions,
    target_offset,
    n_raw_offsets,
    kinematic_chain,
    l_idx1,
    l_idx2,
    face_joint_indx,
):
    src_skel = Skeleton(n_raw_offsets, kinematic_chain, "cpu")
    src_offset = src_skel.get_offsets_joints(torch.from_numpy(positions[0]))
    src_offset = src_offset.numpy()
    tgt_offset = target_offset.numpy()
    # print(src_offset)
    # print(tgt_offset)
    """Calculate Scale Ratio as the ratio of legs"""
    src_leg_len = np.abs(src_offset[l_idx1]).max() + np.abs(src_offset[l_idx2]).max()
    tgt_leg_len = np.abs(tgt_offset[l_idx1]).max() + np.abs(tgt_offset[l_idx2]).max()

    scale_rt = tgt_leg_len / src_leg_len
    # print(scale_rt)
    src_root_pos = positions[:, 0]
    tgt_root_pos = src_root_pos * scale_rt
    """Inverse Kinematics"""
    quat_params = src_skel.inverse_kinematics_np(positions, face_joint_indx)
    # print(quat_params.shape)
    """Forward Kinematics"""
    src_skel.set_offset(target_offset)
    new_joints = src_skel.forward_kinematics_np(quat_params, tgt_root_pos)
    return new_joints


def process_file(
    positions,
    feet_thre,
    tgt_offsets,
    face_joint_indx,
    fid_l,
    fid_r,
    n_raw_offsets,
    kinematic_chain,
    l_idx1,
    l_idx2,
):
    # (seq_len, joints_num, 3)
    #     '''Down Sample'''
    #     positions = positions[::ds_num]
    """Uniform Skeleton"""
    positions = uniform_skeleton(
        positions,
        tgt_offsets,
        n_raw_offsets,
        kinematic_chain,
        l_idx1,
        l_idx2,
        face_joint_indx,
    )
    """Put on Floor"""
    floor_height = positions.min(axis=0).min(axis=0)[1]
    positions[:, :, 1] -= floor_height
    #     print(floor_height)

    #     plot_3d_motion("./positions_1.mp4", kinematic_chain, positions, 'title', fps=20)
    """XZ at origin"""
    root_pos_init = positions[0]
    root_pose_init_xz = root_pos_init[0] * np.array([1, 0, 1])
    positions = positions - root_pose_init_xz

    # '''Move the first pose to origin '''
    # root_pos_init = positions[0]
    # positions = positions - root_pos_init[0]
    """All initially face Z+"""
    r_hip, l_hip, sdr_r, sdr_l = face_joint_indx
    across1 = root_pos_init[r_hip] - root_pos_init[l_hip]
    across2 = root_pos_init[sdr_r] - root_pos_init[sdr_l]
    across = across1 + across2
    across = across / np.sqrt((across**2).sum(axis=-1))[..., np.newaxis]

    # forward (3,), rotate around y-axis
    forward_init = np.cross(np.array([[0, 1, 0]]), across, axis=-1)
    # forward (3,)
    forward_init = (forward_init / np.sqrt((forward_init**2).sum(axis=-1))[..., np.newaxis])

    #     print(forward_init)

    target = np.array([[0, 0, 1]])
    root_quat_init = qbetween_np(forward_init, target)
    root_quat_init = np.ones(positions.shape[:-1] + (4, )) * root_quat_init

    # positions_b = positions.copy()

    positions = qrot_np(root_quat_init, positions)

    #     plot_3d_motion("./positions_2.mp4", kinematic_chain, positions, 'title', fps=20)
    """New ground truth positions"""
    global_positions = positions.copy()

    # plt.plot(positions_b[:, 0, 0], positions_b[:, 0, 2], marker='*')
    # plt.plot(positions[:, 0, 0], positions[:, 0, 2], marker='o', color='r')
    # plt.xlabel('x')
    # plt.ylabel('z')
    # plt.axis('equal')
    # plt.show()
    """ Get Foot Contacts """
    def foot_detect(positions, thres):
        velfactor, _ = np.array([thres, thres]), np.array([3.0, 2.0])

        feet_l_x = (positions[1:, fid_l, 0] - positions[:-1, fid_l, 0])**2
        feet_l_y = (positions[1:, fid_l, 1] - positions[:-1, fid_l, 1])**2
        feet_l_z = (positions[1:, fid_l, 2] - positions[:-1, fid_l, 2])**2
        #     feet_l_h = positions[:-1,fid_l,1]
        #     feet_l = (((feet_l_x + feet_l_y + feet_l_z) < velfactor) & (feet_l_h < heightfactor)).astype(np.float)
        feet_l = ((feet_l_x + feet_l_y + feet_l_z) < velfactor).astype(np.float32)

        feet_r_x = (positions[1:, fid_r, 0] - positions[:-1, fid_r, 0])**2
        feet_r_y = (positions[1:, fid_r, 1] - positions[:-1, fid_r, 1])**2
        feet_r_z = (positions[1:, fid_r, 2] - positions[:-1, fid_r, 2])**2
        #     feet_r_h = positions[:-1,fid_r,1]
        #     feet_r = (((feet_r_x + feet_r_y + feet_r_z) < velfactor) & (feet_r_h < heightfactor)).astype(np.float)
        feet_r = (((feet_r_x + feet_r_y + feet_r_z) < velfactor)).astype(np.float32)
        return feet_l, feet_r

    #
    feet_l, feet_r = foot_detect(positions, feet_thre)
    # feet_l, feet_r = foot_detect(positions, 0.002)
    """Quaternion and Cartesian representation"""
    r_rot = None

    def get_rifke(positions):
        """Local pose"""
        positions[..., 0] -= positions[:, 0:1, 0]
        positions[..., 2] -= positions[:, 0:1, 2]
        """All pose face Z+"""
        positions = qrot_np(np.repeat(r_rot[:, None], positions.shape[1], axis=1), positions)
        return positions

    def get_quaternion(positions, n_raw_offsets, kinematic_chain):
        skel = Skeleton(n_raw_offsets, kinematic_chain, "cpu")
        # (seq_len, joints_num, 4)
        quat_params = skel.inverse_kinematics_np(positions, face_joint_indx, smooth_forward=False)
        """Fix Quaternion Discontinuity"""
        quat_params = qfix(quat_params)
        # (seq_len, 4)
        r_rot = quat_params[:, 0].copy()
        #     print(r_rot[0])
        """Root Linear Velocity"""
        # (seq_len - 1, 3)
        velocity = (positions[1:, 0] - positions[:-1, 0]).copy()
        #     print(r_rot.shape, velocity.shape)
        velocity = qrot_np(r_rot[1:], velocity)
        """Root Angular Velocity"""
        # (seq_len - 1, 4)
        r_velocity = qmul_np(r_rot[1:], qinv_np(r_rot[:-1]))
        quat_params[1:, 0] = r_velocity
        # (seq_len, joints_num, 4)
        return quat_params, r_velocity, velocity, r_rot

    def get_cont6d_params(positions):
        skel = Skeleton(n_raw_offsets, kinematic_chain, "cpu")
        # (seq_len, joints_num, 4)
        quat_params = skel.inverse_kinematics_np(positions, face_joint_indx, smooth_forward=True)
        """Quaternion to continuous 6D"""
        cont_6d_params = quaternion_to_cont6d_np(quat_params)
        # (seq_len, 4)
        r_rot = quat_params[:, 0].copy()
        #     print(r_rot[0])
        """Root Linear Velocity"""
        # (seq_len - 1, 3)
        velocity = (positions[1:, 0] - positions[:-1, 0]).copy()
        #     print(r_rot.shape, velocity.shape)
        velocity = qrot_np(r_rot[1:], velocity)
        """Root Angular Velocity"""
        # (seq_len - 1, 4)
        r_velocity = qmul_np(r_rot[1:], qinv_np(r_rot[:-1]))
        # (seq_len, joints_num, 4)
        return cont_6d_params, r_velocity, velocity, r_rot

    cont_6d_params, r_velocity, velocity, r_rot = get_cont6d_params(positions)
    positions = get_rifke(positions)

    #     trejec = np.cumsum(np.concatenate([np.array([[0, 0, 0]]), velocity], axis=0), axis=0)
    #     r_rotations, r_pos = recover_ric_glo_np(r_velocity, velocity[:, [0, 2]])

    # plt.plot(positions_b[:, 0, 0], positions_b[:, 0, 2], marker='*')
    # plt.plot(ground_positions[:, 0, 0], ground_positions[:, 0, 2], marker='o', color='r')
    # plt.plot(trejec[:, 0], trejec[:, 2], marker='^', color='g')
    # plt.plot(r_pos[:, 0], r_pos[:, 2], marker='s', color='y')
    # plt.xlabel('x')
    # plt.ylabel('z')
    # plt.axis('equal')
    # plt.show()
    """Root height"""
    root_y = positions[:, 0, 1:2]
    """Root rotation and linear velocity"""
    # (seq_len-1, 1) rotation velocity along y-axis
    # (seq_len-1, 2) linear velovity on xz plane
    r_velocity = np.arcsin(r_velocity[:, 2:3])
    l_velocity = velocity[:, [0, 2]]
    #     print(r_velocity.shape, l_velocity.shape, root_y.shape)
    root_data = np.concatenate([r_velocity, l_velocity, root_y[:-1]], axis=-1)
    """Get Joint Rotation Representation"""
    # (seq_len, (joints_num-1) *6) quaternion for skeleton joints
    rot_data = cont_6d_params[:, 1:].reshape(len(cont_6d_params), -1)
    """Get Joint Rotation Invariant Position Represention"""
    # (seq_len, (joints_num-1)*3) local joint position
    ric_data = positions[:, 1:].reshape(len(positions), -1)
    """Get Joint Velocity Representation"""
    # (seq_len-1, joints_num*3)
    local_vel = qrot_np(
        np.repeat(r_rot[:-1, None], global_positions.shape[1], axis=1),
        global_positions[1:] - global_positions[:-1],
    )
    local_vel = local_vel.reshape(len(local_vel), -1)

    data = root_data
    data = np.concatenate([data, ric_data[:-1]], axis=-1)
    data = np.concatenate([data, rot_data[:-1]], axis=-1)
    #     print(data.shape, local_vel.shape)
    data = np.concatenate([data, local_vel], axis=-1)
    data = np.concatenate([data, feet_l, feet_r], axis=-1)
    return data, global_positions, positions, l_velocity


# Recover global angle and positions for rotation data
# root_rot_velocity (B, seq_len, 1)
# root_linear_velocity (B, seq_len, 2)
# root_y (B, seq_len, 1)
# ric_data (B, seq_len, (joint_num - 1)*3)
# rot_data (B, seq_len, (joint_num - 1)*6)
# local_velocity (B, seq_len, joint_num*3)
# foot contact (B, seq_len, 4)
def recover_root_rot_pos(data):
    rot_vel = data[..., 0]
    r_rot_ang = torch.zeros_like(rot_vel).to(data.device)
    """Get Y-axis rotation from rotation velocity"""
    r_rot_ang[..., 1:] = rot_vel[..., :-1]
    r_rot_ang = torch.cumsum(r_rot_ang, dim=-1)

    r_rot_quat = torch.zeros(data.shape[:-1] + (4, )).to(data.device)
    r_rot_quat[..., 0] = torch.cos(r_rot_ang)
    r_rot_quat[..., 2] = torch.sin(r_rot_ang)

    r_pos = torch.zeros(data.shape[:-1] + (3, )).to(data.device)
    r_pos[..., 1:, [0, 2]] = data[..., :-1, 1:3]
    """Add Y-axis rotation to root position"""
    r_pos = qrot(qinv(r_rot_quat), r_pos)

    r_pos = torch.cumsum(r_pos, dim=-2)

    r_pos[..., 1] = data[..., 3]
    return r_rot_quat, r_pos


def recover_from_rot(data, joints_num, skeleton):
    r_rot_quat, r_pos = recover_root_rot_pos(data)

    r_rot_cont6d = quaternion_to_cont6d(r_rot_quat)

    start_indx = 1 + 2 + 1 + (joints_num - 1) * 3
    end_indx = start_indx + (joints_num - 1) * 6
    cont6d_params = data[..., start_indx:end_indx]
    #     print(r_rot_cont6d.shape, cont6d_params.shape, r_pos.shape)
    cont6d_params = torch.cat([r_rot_cont6d, cont6d_params], dim=-1)
    cont6d_params = cont6d_params.view(-1, joints_num, 6)

    positions = skeleton.forward_kinematics_cont6d(cont6d_params, r_pos)

    return positions


def recover_from_ric(data, joints_num):
    r_rot_quat, r_pos = recover_root_rot_pos(data)
    positions = data[..., 4:(joints_num - 1) * 3 + 4]
    positions = positions.view(positions.shape[:-1] + (-1, 3))
    """Add Y-axis rotation to local joints"""
    positions = qrot(qinv(r_rot_quat[..., None, :]).expand(positions.shape[:-1] + (4, )), positions)
    """Add root XZ to joints"""
    positions[..., 0] += r_pos[..., 0:1]
    positions[..., 2] += r_pos[..., 2:3]
    """Concate root and joints"""
    positions = torch.cat([r_pos.unsqueeze(-2), positions], dim=-2)

    return positions


def _get_joints_to_guofeats():
    this_folder = os.path.dirname(os.path.abspath(__file__))
    skeleton_path = os.path.join(this_folder, "skeleton_example_h3d.npy")
    # corresponds to the first frame of "000021"
    # as used in the original ipynb

    # Get offsets of target skeleton
    example_data = torch.from_numpy(np.load(skeleton_path))

    # Lower legs
    l_idx1, l_idx2 = 5, 8
    # Right/Left foot
    fid_r, fid_l = [8, 11], [7, 10]
    # Face direction, r_hip, l_hip, sdr_r, sdr_l
    face_joint_indx = [2, 1, 17, 16]
    # l_hip, r_hip
    joints_num = 22

    n_raw_offsets = torch.from_numpy(t2m_raw_offsets)
    kinematic_chain = t2m_kinematic_chain

    tgt_skel = Skeleton(n_raw_offsets, kinematic_chain, "cpu")
    # (joints_num, 3)
    tgt_offsets = tgt_skel.get_offsets_joints(example_data)

    # print(tgt_offsets)

    def transform(source_data):
        source_data = source_data[:, :joints_num].copy()
        data, ground_positions, positions, l_velocity = process_file(
            source_data,
            0.002,
            tgt_offsets,
            face_joint_indx,
            fid_l,
            fid_r,
            n_raw_offsets,
            kinematic_chain,
            l_idx1,
            l_idx2,
        )
        return data

    return transform


def _get_guofeats_to_joints():
    joints_num = 22

    def transform(data):
        joints = recover_from_ric(data, joints_num)
        # put back Z as the third coordinate:
        # swap Y and Z
        # and minus Y
        x, z, my = torch.unbind(joints, axis=-1)
        return torch.stack((x, -my, z), axis=-1)

    return transform


joints_to_guofeats = _get_joints_to_guofeats()
guofeats_to_joints = _get_guofeats_to_joints()


def rearrange_guofeats(x: np.ndarray):
    num_joints = 22

    ric_data_offset = 4
    rot_data_offset = ric_data_offset + 3 * (num_joints - 1)
    local_vel_offset = rot_data_offset + 6 * (num_joints - 1)
    feet_contact_offset = local_vel_offset + 3 * num_joints

    assert feet_contact_offset + 4 == x.shape[-1]

    def get_ric_rot_vel(part_index: list[int]):
        ric_data = np.concatenate([x[..., ric_data_offset + 3 * (joint_index - 1):ric_data_offset + 3 * joint_index] for joint_index in part_index], axis=-1)
        rot_data = np.concatenate([x[..., rot_data_offset + 6 * (joint_index - 1):rot_data_offset + 6 * joint_index] for joint_index in part_index], axis=-1)
        local_vel_data = np.concatenate([x[..., local_vel_offset + 3 * joint_index:local_vel_offset + 3 * (joint_index + 1)] for joint_index in part_index], axis=-1)

        return np.concatenate([ric_data, rot_data, local_vel_data], axis=-1)

    head_part_index = [12, 15]
    left_arm_part_index = [13, 16, 18, 20]
    right_arm_part_index = [14, 17, 19, 21]
    torso_part_index = [0, 3, 6, 9]
    left_leg_part_index = [1, 4, 7, 10]
    right_leg_part_index = [2, 5, 8, 11]

    head_features = get_ric_rot_vel(head_part_index)
    left_arm_features = get_ric_rot_vel(left_arm_part_index)
    right_arm_features = get_ric_rot_vel(right_arm_part_index)
    torso_features = get_ric_rot_vel(torso_part_index[1:])
    left_leg_features = get_ric_rot_vel(left_leg_part_index)
    right_leg_features = get_ric_rot_vel(right_leg_part_index)

    torso_features = np.concatenate([torso_features, x[..., 0:4], x[..., local_vel_offset:local_vel_offset + 3]], axis=-1)
    left_leg_features = np.concatenate([left_leg_features, x[..., feet_contact_offset:feet_contact_offset + 2]], axis=-1)
    right_leg_features = np.concatenate([right_leg_features, x[..., feet_contact_offset + 2:feet_contact_offset + 4]], axis=-1)

    return {
        "head": head_features,
        "left_arm": left_arm_features,
        "right_arm": right_arm_features,
        "torso": torso_features,
        "left_leg": left_leg_features,
        "right_leg": right_leg_features,
    }


from torch import Tensor


def recover_rearranged_guofeats(x: dict[str, Tensor]):
    num_joints = 22

    ric_data_offset = 4
    rot_data_offset = ric_data_offset + 3 * (num_joints - 1)
    local_vel_offset = rot_data_offset + 6 * (num_joints - 1)
    feet_contact_offset = local_vel_offset + 3 * num_joints

    assert feet_contact_offset + 4 == 263

    head_part_index = [12, 15]
    left_arm_part_index = [13, 16, 18, 20]
    right_arm_part_index = [14, 17, 19, 21]
    torso_part_index = [0, 3, 6, 9]
    left_leg_part_index = [1, 4, 7, 10]
    right_leg_part_index = [2, 5, 8, 11]

    result = torch.empty(*x["head"].shape[:-1], 263)
    result[..., 0:4] = x["torso"][..., -7:-3]
    result[..., local_vel_offset:local_vel_offset + 3] = x["torso"][..., -3:]
    result[..., -4:-2] = x["left_leg"][..., -2:]
    result[..., -2:] = x["right_leg"][..., -2:]

    def set_ric_rot_vel(part_index: list[int], features: Tensor):
        _ric_data_offset = 0
        _rot_data_offset = _ric_data_offset + 3 * len(part_index)
        _local_vel_offset = _rot_data_offset + 6 * len(part_index)
        for index, joint_index in enumerate(part_index):
            result[..., ric_data_offset + 3 * (joint_index - 1):ric_data_offset + 3 * joint_index] = features[..., _ric_data_offset + 3 * index:_ric_data_offset + 3 * (index + 1)]
            result[..., rot_data_offset + 6 * (joint_index - 1):rot_data_offset + 6 * joint_index] = features[..., _rot_data_offset + 6 * index:_rot_data_offset + 6 * (index + 1)]
            result[..., local_vel_offset + 3 * joint_index:local_vel_offset + 3 * (joint_index + 1)] = features[..., _local_vel_offset + 3 * index:_local_vel_offset + 3 * (index + 1)]

    set_ric_rot_vel(head_part_index, x["head"])
    set_ric_rot_vel(left_arm_part_index, x["left_arm"])
    set_ric_rot_vel(right_arm_part_index, x["right_arm"])
    set_ric_rot_vel(torso_part_index[1:], x["torso"])
    set_ric_rot_vel(left_leg_part_index, x["left_leg"])
    set_ric_rot_vel(right_leg_part_index, x["right_leg"])

    return result


if __name__ == "__main__":
    x = np.empty((1, 263))
    for key, value in rearrange_guofeats(x).items():
        print(f"\"{key}\": {value.shape[-1]}")
