import argparse
import os
import random
from pathlib import Path
import lightning as L
from lightning.fabric import Fabric
import numpy as np
import torch
from typing import List
import torch.nn as nn
import torch.nn.functional as F
import torchvision
import wandb
from torch.utils.data import DataLoader
from torchvision.utils import save_image
from tqdm import tqdm
from lightning.fabric.loggers import TensorBoardLogger
from dataset.dresscode import DressCodeDataset
from dataset.vitonhd import VitonHDDataset
from models.ConvNet_TPS import ConvNet_TPS
from models.UNet import UNetVanilla
from utils.vgg_loss import VGGLoss

random.seed(1234)
np.random.seed(1234)
torch.manual_seed(1234)
PROJECT_ROOT = Path(__file__).absolute().parents[1].absolute()
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class SubsetDataset(torch.utils.data.Dataset):
    """
    create a subset of the torch dataset
    """

    def __init__(self, dataset: torch.utils.data.Dataset, indices: List[int]):
        self.dataset = dataset
        self.indices = indices

    def __getitem__(self, index):
        return self.dataset[self.indices[index]]

    def __len__(self):
        return len(self.indices)


@torch.no_grad()
def compute_metric(dataloader: DataLoader, tps: ConvNet_TPS, criterion_l1: nn.L1Loss, criterion_vgg: VGGLoss,
                   refinement: UNetVanilla = None, height: int = 512, width: int = 384) -> tuple[
    float, float, list[list]]:
    """
    Perform inference on the given dataloader and compute the L1 and VGG loss between the warped cloth and the
    ground truth image.
    """
    tps.eval()
    if refinement:  # will be there for the refinement finetuining step
        refinement.eval()

    running_loss = 0.
    vgg_running_loss = 0
    for step, inputs in enumerate(tqdm(dataloader)):
        cloth = inputs['cloth']
        image = inputs['image']
        im_cloth = inputs['im_cloth']
        im_mask = inputs['im_mask']
        pose_map = inputs.get('dense_uv')
        if pose_map is None:
            pose_map = inputs['pose_map']
        pose_map = pose_map

        # for the TPS training, data loader is already giving (256, 192) resolution images
        # TPS parameters prediction
        # For sake of performance, the TPS parameters are predicted on a low resolution image
        low_cloth = torchvision.transforms.functional.resize(cloth, (256, 192),
                                                             torchvision.transforms.InterpolationMode.BILINEAR,
                                                             antialias=True)
        low_im_mask = torchvision.transforms.functional.resize(im_mask, (256, 192),
                                                               torchvision.transforms.InterpolationMode.BILINEAR,
                                                               antialias=True)
        low_pose_map = torchvision.transforms.functional.resize(pose_map, (256, 192),
                                                                torchvision.transforms.InterpolationMode.BILINEAR,
                                                                antialias=True)

        # TPS parameters prediction
        agnostic = torch.cat([low_im_mask, low_pose_map], 1)

        low_grid, theta, rx, ry, cx, cy, rg, cg = tps(low_cloth, agnostic)
        # upsample the tps predicted grid to the (512, 384)
        # We upsample the grid to the original image size and warp the cloth using the predicted TPS parameters
        highres_grid = torchvision.transforms.functional.resize(low_grid.permute(0, 3, 1, 2),
                                                                size=(height, width),
                                                                interpolation=torchvision.transforms.InterpolationMode.BILINEAR,
                                                                antialias=True).permute(0, 2, 3, 1)
        warped_cloth = F.grid_sample(cloth, highres_grid, padding_mode='border')

        # for tps training this is None
        if refinement:
            # Refine the warped cloth using the refinement network
            warped_cloth = torch.cat([im_mask, pose_map, warped_cloth], 1)
            warped_cloth = refinement(warped_cloth)

        # Compute the loss
        loss = criterion_l1(warped_cloth, im_cloth)
        running_loss += loss.item()
        if criterion_vgg:
            vgg_loss = criterion_vgg(warped_cloth, im_cloth)
            vgg_running_loss += vgg_loss.item()

    # gather from all the processes to compute per epoch loss
    running_loss = fabric.all_gather(running_loss).sum() / len(dataloader.dataset)
    vgg_running_loss = fabric.all_gather(vgg_running_loss).sum() / len(dataloader.dataset)

    visual = [[image, cloth, im_cloth, warped_cloth.clamp(-1, 1)]]
    # loss = running_loss / (step + 1)
    # vgg_loss = vgg_running_loss / (step + 1)
    # return loss, vgg_loss, visual
    return running_loss, vgg_running_loss, visual


def training_loop_tps(dataloader: DataLoader, tps: ConvNet_TPS, optimizer_tps: torch.optim.Optimizer,
                      criterion_l1: nn.L1Loss, scaler: torch.cuda.amp.GradScaler, const_weight: float) -> tuple[
    float, float, float, list[list]]:
    """
    Training loop for the TPS network. Note that the TPS is trained on a low resolution image for sake of performance.
    """
    tps.train()
    running_loss = 0.
    running_l1_loss = 0.
    running_const_loss = 0.
    for step, inputs in enumerate(tqdm(dataloader)):  # Yield images with low resolution (256x192)
        low_cloth = inputs['cloth']
        low_image = inputs['image']
        low_im_cloth = inputs['im_cloth']
        low_im_mask = inputs['im_mask']

        low_pose_map = inputs.get('dense_uv')
        if low_pose_map is None:  # If the dataset does not provide dense UV maps, use the pose map (keypoints) instead
            low_pose_map = inputs['pose_map']
        low_pose_map = low_pose_map

        # with torch.cuda.amp.autocast():
        with fabric.autocast():
            # TPS parameters prediction
            agnostic = torch.cat([low_im_mask, low_pose_map], 1)
            low_grid, theta, rx, ry, cx, cy, rg, cg = tps(low_cloth, agnostic)

            # Warp the cloth using the predicted TPS parameters
            low_warped_cloth = F.grid_sample(low_cloth, low_grid, padding_mode='border')

            # Compute the loss
            l1_loss = criterion_l1(low_warped_cloth, low_im_cloth)
            const_loss = torch.mean(rx + ry + cx + cy + rg + cg)

            loss = l1_loss + const_loss * const_weight

        # Update the parameters
        optimizer_tps.zero_grad()
        # scaler.scale(loss).backward()
        fabric.backward(loss)
        # scaler.step(optimizer_tps)
        optimizer_tps.step()
        # scaler.update()

        running_loss += loss.item()
        running_l1_loss += l1_loss.item()
        running_const_loss += const_loss.item()

    visual = [[low_image, low_cloth, low_im_cloth, low_warped_cloth.clamp(-1, 1)]]
    running_loss = fabric.all_gather(running_loss).sum() / len(dataloader.dataset)
    running_l1_loss = fabric.all_gather(running_l1_loss).sum() / len(dataloader.dataset)
    running_const_loss = fabric.all_gather(running_const_loss).sum() / len(dataloader.dataset)

    # loss = running_loss / (step + 1)
    # l1_loss = running_l1_loss / (step + 1)
    # const_loss = running_const_loss / (step + 1)
    # return loss, l1_loss, const_loss, visual
    return running_loss, running_l1_loss, running_const_loss, visual


def training_loop_refinement(dataloader: DataLoader, tps: ConvNet_TPS, refinement: UNetVanilla,
                             optimizer_ref: torch.optim.Optimizer, criterion_l1: nn.L1Loss, criterion_vgg: VGGLoss,
                             l1_weight: float, vgg_weight: float, scaler: torch.cuda.amp.GradScaler, height=512,
                             width=384) -> tuple[float, float, float, list[list]]:
    """
    Training loop for the refinement network. Note that the refinement network is trained on a high resolution image
    """
    tps.eval()
    refinement.train()
    running_loss = 0.
    running_l1_loss = 0.
    running_vgg_loss = 0.
    for step, inputs in enumerate(tqdm(dataloader)):
        cloth = inputs['cloth']
        image = inputs['image']
        im_cloth = inputs['im_cloth']
        im_mask = inputs['im_mask']

        pose_map = inputs.get('dense_uv')
        if pose_map is None:  # If the dataset does not provide dense UV maps, use the pose map (keypoints) instead
            pose_map = inputs['pose_map']
        pose_map = pose_map
        # dataloader will be giving (512, 384) resolution images, so no need to resize for tps
        # Resize the inputs to the low resolution for the TPS network
        low_cloth = torchvision.transforms.functional.resize(cloth, (256, 192),
                                                             torchvision.transforms.InterpolationMode.BILINEAR,
                                                             antialias=True)
        low_im_mask = torchvision.transforms.functional.resize(im_mask, (256, 192),
                                                               torchvision.transforms.InterpolationMode.BILINEAR,
                                                               antialias=True)
        low_pose_map = torchvision.transforms.functional.resize(pose_map, (256, 192),
                                                                torchvision.transforms.InterpolationMode.BILINEAR,
                                                                antialias=True)

        with fabric.autocast():
            # TPS parameters prediction
            agnostic = torch.cat([low_im_mask, low_pose_map], 1)

            low_grid, theta, rx, ry, cx, cy, rg, cg = tps(low_cloth, agnostic)
            low_warped_cloth = F.grid_sample(cloth, low_grid, padding_mode='border')

            # We upsample the grid to the original image size and warp the cloth using the predicted TPS parameters
            highres_grid = torchvision.transforms.functional.resize(low_grid.permute(0, 3, 1, 2),
                                                                    size=(height, width),
                                                                    interpolation=torchvision.transforms.InterpolationMode.BILINEAR,
                                                                    antialias=True).permute(0, 2, 3, 1)

            warped_cloth = F.grid_sample(cloth, highres_grid, padding_mode='border')

            # Refine the warped cloth using the refinement network
            warped_cloth = torch.cat([im_mask, pose_map, warped_cloth], 1)
            warped_cloth = refinement(warped_cloth)

            # Compute the loss
            l1_loss = criterion_l1(warped_cloth, im_cloth)
            vgg_loss = criterion_vgg(warped_cloth, im_cloth)

            loss = l1_loss * l1_weight + vgg_loss * vgg_weight

        # Update the parameters
        optimizer_ref.zero_grad()  # zero the gradients
        fabric.backward(loss)  # calculate the gradients
        # scaler.scale(loss).backward()
        # scaler.step(optimizer_ref)
        optimizer_ref.step()  # update the parameters
        # scaler.update()

        running_loss += loss.item()
        running_l1_loss += l1_loss.item()
        running_vgg_loss += vgg_loss.item()

    running_loss = fabric.all_gather(running_loss).sum() / len(dataloader.dataset)
    running_l1_loss = fabric.all_gather(running_l1_loss).sum() / len(dataloader.dataset)
    running_vgg_loss = fabric.all_gather(running_vgg_loss).sum() / len(dataloader.dataset)

    visual = [[image, cloth, im_cloth, low_warped_cloth.clamp(-1, 1)]]
    # loss = running_loss / (step + 1)
    # l1_loss = running_l1_loss / (step + 1)
    # vgg_loss = running_vgg_loss / (step + 1)
    # return loss, l1_loss, vgg_loss, visual
    return running_loss, running_l1_loss, running_vgg_loss, visual


@torch.no_grad()
def extract_images(dataloader: DataLoader, tps: ConvNet_TPS, refinement: UNetVanilla, save_path: str, height: int = 512,
                   width: int = 384) -> None:
    """
    Extracts the images using the trained networks and saves them to the save_path
    """
    tps.eval()
    refinement.eval()

    # running_loss = 0.
    for step, inputs in enumerate(tqdm(dataloader)):
        c_name = inputs['c_name']
        im_name = inputs['im_name']
        cloth = inputs['cloth']
        category = inputs.get('category')
        im_mask = inputs['im_mask']
        pose_map = inputs.get('dense_uv')
        if pose_map is None:
            pose_map = inputs['pose_map']
        pose_map = pose_map

        # Resize the inputs to the low resolution for the TPS network
        low_cloth = torchvision.transforms.functional.resize(cloth, (256, 192),
                                                             torchvision.transforms.InterpolationMode.BILINEAR,
                                                             antialias=True)
        low_im_mask = torchvision.transforms.functional.resize(im_mask, (256, 192),
                                                               torchvision.transforms.InterpolationMode.BILINEAR,
                                                               antialias=True)
        low_pose_map = torchvision.transforms.functional.resize(pose_map, (256, 192),
                                                                torchvision.transforms.InterpolationMode.BILINEAR,
                                                                antialias=True)

        # TPS parameters prediction
        agnostic = torch.cat([low_im_mask, low_pose_map], 1)

        low_grid, theta, rx, ry, cx, cy, rg, cg = tps(low_cloth, agnostic)
        # upsample to resolution : (512, 384)
        # We upsample the grid to the original image size and warp the cloth using the predicted TPS parameters
        highres_grid = torchvision.transforms.functional.resize(low_grid.permute(0, 3, 1, 2),
                                                                size=(height, width),
                                                                interpolation=torchvision.transforms.InterpolationMode.BILINEAR,
                                                                antialias=True).permute(0, 2, 3, 1)

        warped_cloth = F.grid_sample(cloth, highres_grid, padding_mode='border')

        # Refine the warped cloth using the refinement network
        warped_cloth = torch.cat([im_mask, pose_map, warped_cloth], 1)
        warped_cloth = refinement(warped_cloth)

        warped_cloth = (warped_cloth + 1) / 2
        warped_cloth = warped_cloth.clamp(0, 1)

        # Save the images
        for cname, iname, warpclo, cat in zip(c_name, im_name, warped_cloth, category):
            if not os.path.exists(os.path.join(save_path, cat)):
                os.makedirs(os.path.join(save_path, cat))
            save_image(warpclo, os.path.join(save_path, cat, iname.replace(".jpg", "") + "_" + cname),
                       quality=95)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, required=True, choices=["dresscode", "vitonhd", "combined"],
                        help="dataset to use")
    parser.add_argument('--dresscode_dataroot', type=str, help='DressCode dataroot')
    parser.add_argument('--checkpoints_dir', type=str, default=str(PROJECT_ROOT / "TPS_checkpoints"))
    parser.add_argument('--vitonhd_dataroot', type=str, help='VitonHD dataroot')
    parser.add_argument('--exp_name', type=str, required=True, help='experiment name')
    parser.add_argument('-b', '--batch_size', type=int, default=16, help='train/test batch size')
    parser.add_argument('-j', '--workers', type=int, default=10, help='number of data loading workers')
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--width", type=int, default=384)
    parser.add_argument('--const_weight', type=float, default=0.01, help='weight for the TPS constraint loss')
    parser.add_argument('--lr', type=float, default=1e-4, help='initial learning rate for adam')
    parser.add_argument('--wandb_log', default=True, action='store_true', help='use wandb to log the training')
    parser.add_argument('--wandb_project', type=str, default="LaDI_VTON_tps", help='wandb project name')
    parser.add_argument('--wandb_entity', type=str, help='wandb entity name')
    parser.add_argument('--dense', dest='dense', default=False, action='store_true', help='use dense uv map')
    parser.add_argument("--only_extraction", default=False, action='store_true',
                        help="only extract the images using the trained networks without training")
    parser.add_argument('--vgg_weight', type=float, default=0.25, help='weight for the VGG loss (refinement network)')
    parser.add_argument('--l1_weight', type=float, default=1, help='weight for the L1 loss (refinement network)')
    parser.add_argument('--save_path', type=str, help='path to save the warped cloth images (if not provided, '
                                                      'the images will be saved in the data folder)')
    parser.add_argument('--epochs_tps', type=int, default=50, help='number of epochs to train the TPS network')
    parser.add_argument('--epochs_refinement', type=int, default=50,
                        help='number of epochs to train the refinement network')

    # add store true argument --dry_test
    parser.add_argument('--dry_test', dest='dry_test', default=False, action='store_true',
                        help='dry run test with 1000 samples for train and test')
    args = parser.parse_args()
    return args


# ==========================================================================
#                             ⚡ Main process
# ==========================================================================

def main(logger, fabric):
    args = parse_args()
    fabric.print(args.exp_name)

    # save the args to logger (tensorboard)
    if fabric.is_global_zero:
        logger.log_hyperparams(vars(args))

    fabric.barrier()

    if args.dataset == "vitonhd" and args.vitonhd_dataroot is None:
        raise ValueError("VitonHD dataroot must be provided")
    if args.dataset == "dresscode" and args.dresscode_dataroot is None:
        raise ValueError("DressCode dataroot must be provided")
    if args.dataset == "combined" and args.dresscode_dataroot is None and args.vitonhd_dataroot is None:
        raise ValueError("DressCode dataroot and VITON-HD must be provided")

    # Enable wandb logging
    if args.wandb_log:
        if fabric.is_global_zero:
            wandb.init(project=args.wandb_project, entity=args.wandb_entity, name=args.exp_name)
            wandb.config.update(vars(args))
        fabric.barrier()

    dataset_output_list = ['c_name', 'im_name', 'cloth', 'image', 'im_cloth', 'im_mask', 'pose_map', 'category']
    if args.dense:
        dataset_output_list.append('dense_uv')

    # ======================================================================
    #                   training datasets
    # ======================================================================

    # Training dataset and dataloader
    # if args.dataset == "vitonhd":
    dataset_train_vton = VitonHDDataset(phase='train',
                                        outputlist=dataset_output_list,
                                        dataroot_path=args.vitonhd_dataroot,
                                        size=(args.height, args.width))
    # elif args.dataset == "dresscode":
    dataset_train_dress = DressCodeDataset(dataroot_path=args.dresscode_dataroot,
                                           phase='train',
                                           outputlist=dataset_output_list,
                                           size=(args.height, args.width))
    # else:
    #     raise NotImplementedError("Dataset should be either vitonhd or dresscode")
    combined_train_ds = torch.utils.data.ConcatDataset([dataset_train_vton, dataset_train_dress])

    if args.dry_test:
        # ========================= SUBSET for testing =========================
        import random
        # create a random list of 1000 integers between range 0 to len(combined_train_ds)

        indices = random.sample(range(0, len(combined_train_ds)), 1000)

        combined_train_ds = torch.utils.data.Subset(combined_train_ds, indices)
    # ========================= SUBSET for testing =========================

    dataloader_train = DataLoader(batch_size=args.batch_size,
                                  dataset=combined_train_ds,
                                  shuffle=True,
                                  num_workers=args.workers)

    dataloader_train = fabric.setup_dataloaders(dataloader_train)
    # ======================================================================
    #                   val datasets
    # ======================================================================

    # Validation dataset and dataloader
    # if args.dataset == "vitonhd":
    dataset_test_paired_viton = VitonHDDataset(phase='test',
                                               dataroot_path=args.vitonhd_dataroot,
                                               outputlist=dataset_output_list, size=(args.height, args.width))

    dataset_test_unpaired_viton = VitonHDDataset(phase='test',
                                                 order='unpaired',
                                                 dataroot_path=args.vitonhd_dataroot,
                                                 outputlist=dataset_output_list, size=(args.height, args.width))

    # elif args.dataset == "dresscode":
    dataset_test_paired_dress = DressCodeDataset(dataroot_path=args.dresscode_dataroot,
                                                 phase='test',
                                                 outputlist=dataset_output_list, size=(args.height, args.width))

    dataset_test_unpaired_dress = DressCodeDataset(phase='test',
                                                   order='unpaired',
                                                   dataroot_path=args.dresscode_dataroot,
                                                   outputlist=dataset_output_list, size=(args.height, args.width))

    # else:
    #     raise NotImplementedError("Dataset should be either vitonhd or dresscode")

    combined_test_ds = torch.utils.data.ConcatDataset([
        dataset_test_paired_viton,
        # dataset_test_unpaired_viton,
        dataset_test_paired_dress,
        # dataset_test_unpaired_dress
    ])
    if args.dry_test:
        # ========================= SUBSET for testing =========================
        import random
        # create a random list of 1000 integers between range 0 to len(combined_train_ds)

        indices = random.sample(range(0, len(combined_test_ds)), 1000)

        combined_test_ds = torch.utils.data.Subset(combined_test_ds, indices)
        # ========================= SUBSET for testing =========================

    dataloader_test = DataLoader(batch_size=args.batch_size,
                                 dataset=combined_test_ds,
                                 shuffle=True,
                                 num_workers=args.workers, drop_last=True)

    dataloader_test = fabric.setup_dataloaders(dataloader_test)

    # ====================================================
    #               define models
    # ====================================================

    # Define TPS and refinement network
    input_nc = 5 if args.dense else 21
    n_layer = 3
    tps = ConvNet_TPS(256, 192, input_nc, n_layer)

    refinement = UNetVanilla(
        n_channels=8 if args.dense else 24,
        n_classes=3,
        bilinear=True)

    # Define optimizer, scaler and loss
    optimizer_tps = torch.optim.Adam(tps.parameters(), lr=args.lr, betas=(0.5, 0.99))
    optimizer_ref = torch.optim.Adam(list(refinement.parameters()), lr=args.lr, betas=(0.5, 0.99))

    # 🔴 put models to proper devices
    tps, optimizer_tps = fabric.setup(tps, optimizer_tps)
    refinement, optimizer_ref = fabric.setup(refinement, optimizer_ref)

    # scaler = torch.cuda.amp.GradScaler()
    criterion_l1 = nn.L1Loss()

    if args.vgg_weight > 0:
        criterion_vgg = VGGLoss().to(fabric.device)
    else:
        criterion_vgg = None

    start_epoch = 0

    # ============================================================
    #             load from pre-trained ckpt logic
    # ============================================================

    if os.path.exists(os.path.join(args.checkpoints_dir, args.exp_name, f"checkpoint_last.pth")):
        fabric.print('Loading full checkpoint from pretrained ckpt ....')

        # REFERENCE: https://lightning.ai/docs/fabric/stable/guide/checkpoint.html
        EPOCH = None
        # define the state to load from
        _state = {

            'tps': tps,
            'refinement': refinement,
            'optimizer_tps': optimizer_tps,
            'optimizer_ref': optimizer_ref,
            'epoch': EPOCH
        }
        # load the checkpoint on all devices
        fabric.load(os.path.join(args.checkpoints_dir, args.exp_name, f"checkpoint_last.pth"),
                    _state)
        # state_dict = torch.load(os.path.join(args.checkpoints_dir, args.exp_name, f"checkpoint_last.pth"))
        # tps.load_state_dict(state_dict['tps'])
        # refinement.load_state_dict(state_dict['refinement'])
        # optimizer_tps.load_state_dict(state_dict['optimizer_tps'])
        # optimizer_ref.load_state_dict(state_dict['optimizer_ref'])
        # start_epoch = state_dict['epoch']

        if args.only_extraction:
            fabric.print("Extracting warped cloth images...")
            # extraction_dataset_paired = torch.utils.data.ConcatDataset([dataset_test_paired, dataset_train])
            # extraction_dataloader_paired = DataLoader(batch_size=args.batch_size,
            #                                           dataset=extraction_dataset_paired,
            #                                           shuffle=False,
            #                                           num_workers=args.workers,
            #                                           drop_last=False)
            extraction_dataloader_paired = dataloader_test

            if args.save_path:
                warped_cloth_root = args.save_path
            else:
                warped_cloth_root = PROJECT_ROOT / 'data'

            save_name_paired = warped_cloth_root / 'warped_cloths' / args.dataset
            fabric.print(f"extracting to {save_name_paired}")
            extract_images(extraction_dataloader_paired, tps, refinement, save_name_paired, args.height, args.width)

            # since we combined all the test datasets so no need for this

            # extraction_dataset = dataset_test_unpaired
            # extraction_dataloader_paired = DataLoader(batch_size=args.batch_size,
            #                                           dataset=extraction_dataset,
            #                                           shuffle=False,
            #                                           num_workers=args.workers)
            #
            # save_name_unpaired = warped_cloth_root / 'warped_cloths_unpaired' / args.dataset
            # extract_images(extraction_dataloader_paired, tps, refinement, save_name_unpaired, args.height, args.width)
            exit()

    if args.only_extraction and not os.path.exists(
            os.path.join(args.checkpoints_dir, args.exp_name, f"checkpoint_last.pth")):
        print("No checkpoint found, before extracting warped cloth images, please train the model first.")
        exit()

    # ===============================================================
    #                         TPS training loop
    # ===============================================================
    # Training loop for TPS training
    # Set training dataset height and width to (256, 192) since the TPS is trained using a lower resolution
    dataset_train_vton.height = 256
    dataset_train_dress.height = 256
    dataset_train_vton.width = 192
    dataset_train_dress.width = 192

    for e in range(start_epoch, args.epochs_tps):
        fabric.print(f"Epoch {e}/{args.epochs_tps}")
        fabric.print('train')
        # one epoch for training
        if args.dry_test:
            batch = next(iter(dataloader_train))
            fabric.print(batch['im_cloth'].shape)

        train_loss, train_l1_loss, train_const_loss, visual = training_loop_tps(
            dataloader_train,
            tps,
            optimizer_tps,
            criterion_l1,
            None,
            args.const_weight)

        # one epoch for validation metrics
        # Compute loss on paired test set
        fabric.print('paired/unpaired combined metrics')
        running_loss, vgg_running_loss, visual = compute_metric(
            # dataloader_test_paired
            dataloader_test,
            tps,
            criterion_l1,
            criterion_vgg,
            refinement=None,
            height=args.height,
            width=args.width)

        imgs = torchvision.utils.make_grid(torch.cat(visual[0]), nrow=len(visual[0][0]), padding=2, normalize=True,
                                           value_range=None, scale_each=False, pad_value=0)

        # # Compute loss on unpaired test set
        # print('unpaired test')
        # running_loss_unpaired, vgg_running_loss_unpaired, visual = compute_metric(
        #     dataloader_test_unpaired,
        #     tps,
        #     criterion_l1,
        #     criterion_vgg,
        #     refinement=None,
        #     height=args.height,
        #     width=args.width)
        #
        # imgs_unpaired = torchvision.utils.make_grid(torch.cat(visual[0]), nrow=len(visual[0][0]), padding=2,
        #                                             normalize=True, range=None,
        #                                             scale_each=False, pad_value=0)

        # Log to wandb
        if args.wandb_log:
            if fabric.is_global_zero:
                wandb.log({
                    'train/loss': train_loss,
                    'train/l1_loss': train_l1_loss,
                    'train/const_loss': train_const_loss,
                    'train/vgg_loss': 0,
                    'eval/eval_loss_paired_unpaired': running_loss,
                    'eval/eval_vgg_loss_paired_unpaired': vgg_running_loss,
                    # 'eval/eval_loss_unpaired': running_loss_unpaired,
                    # 'eval/eval_vgg_loss_unpaired': vgg_running_loss_unpaired,
                    'images_paired_unpaired': wandb.Image(imgs),
                    # 'images_unpaired': wandb.Image(imgs_unpaired),
                })
            fabric.barrier()

        # =========================================================================
        #                       save the checkpoint
        # =========================================================================
        # Save checkpoint
        os.makedirs(os.path.join(args.checkpoints_dir, args.exp_name), exist_ok=True)
        to_save_state = {

            'tps': tps,
            'refinement': refinement,
            'optimizer_tps': optimizer_tps,
            'optimizer_ref': optimizer_ref,
            'epoch': e + 1
        }
        fabric.save(os.path.join(args.checkpoints_dir, args.exp_name, f"checkpoint_last.pth"), to_save_state)
        # torch.save({
        #     'epoch': e + 1,
        #     'tps': tps.state_dict(),
        #     'refinement': refinement.state_dict(),
        #     'optimizer_tps': optimizer_tps.state_dict(),
        #     'optimizer_ref': optimizer_ref.state_dict(),
        # }, os.path.join(args.checkpoints_dir, args.exp_name, f"checkpoint_last.pth"))

    # =========================================================================
    #                 TPS training ends here trained model will be put to
    #    eval. mode and refinement network training will start from here
    # =========================================================================

    # scaler = torch.cuda.amp.GradScaler()  # Initialize scaler again for refinement

    # Training loop for refinement
    # Set training dataset height and width to (args.height, args.width) since the refinement is trained using a higher resolution
    # combined_train_ds.height = args.height # set to 512 x 384
    # combined_train_ds.width = args.width

    dataset_train_vton.height = args.height
    dataset_train_dress.height = args.height
    dataset_train_vton.width = args.width
    dataset_train_dress.width = args.width

    for e in range(max(start_epoch, args.epochs_tps), max(start_epoch, args.epochs_tps) + args.epochs_refinement):
        print(f"Epoch {e}/{max(start_epoch, args.epochs_tps) + args.epochs_refinement}")
        train_loss, train_l1_loss, train_vgg_loss, visual = training_loop_refinement(
            dataloader_train,
            tps,
            refinement,
            optimizer_ref,
            criterion_l1,
            criterion_vgg,
            args.l1_weight,
            args.vgg_weight,
            None,
            args.height,
            args.width)

        # Compute loss on paired test set
        running_loss, vgg_running_loss, visual = compute_metric(
            dataloader_test,
            tps,
            criterion_l1,
            criterion_vgg,
            refinement=refinement,
            height=args.height,
            width=args.width)

        imgs = torchvision.utils.make_grid(torch.cat(visual[0]), nrow=len(visual[0][0]), padding=2, normalize=True,
                                           value_range=None, scale_each=False, pad_value=0)

        # # Compute loss on unpaired test set
        # running_loss_unpaired, vgg_running_loss_unpaired, visual = compute_metric(
        #     dataloader_test_unpaired,
        #     tps,
        #     criterion_l1,
        #     criterion_vgg,
        #     refinement=refinement,
        #     height=args.height,
        #     width=args.width)
        #
        # imgs_unpaired = torchvision.utils.make_grid(torch.cat(visual[0]), nrow=len(visual[0][0]), padding=2,
        #                                             normalize=True, range=None,
        #                                             scale_each=False, pad_value=0)

        # Log to wandb
        if args.wandb_log:
            if fabric.is_global_zero:
                wandb.log({
                    'train/loss': train_loss,
                    'train/l1_loss': train_l1_loss,
                    'train/const_loss': 0,
                    'train/vgg_loss': train_vgg_loss,
                    'eval/eval_loss_paired_unpaired': running_loss,
                    'eval/eval_vgg_loss_paired_unpaired': vgg_running_loss,
                    # 'eval/eval_loss_unpaired': running_loss_unpaired,
                    # 'eval/eval_vgg_loss_unpaired': vgg_running_loss_unpaired,
                    'images_paired_unpaired': wandb.Image(imgs),
                    # 'images_unpaired': wandb.Image(imgs_unpaired),
                })
            fabric.barrier()

        # Save checkpoint
        os.makedirs(os.path.join(args.checkpoints_dir, args.exp_name), exist_ok=True)
        # torch.save({
        #     'epoch': e + 1,
        #     'tps': tps.state_dict(),
        #     'refinement': refinement.state_dict(),
        #     'optimizer_tps': optimizer_tps.state_dict(),
        #     'optimizer_ref': optimizer_ref.state_dict(),
        # }, os.path.join(args.checkpoints_dir, args.exp_name, f"checkpoint_last.pth"))

        to_save_state_refine = {

            'tps': tps,
            'refinement': refinement,
            'optimizer_tps': optimizer_tps,
            'optimizer_ref': optimizer_ref,
            'epoch': e + 1
        }
        fabric.save(os.path.join(args.checkpoints_dir, args.exp_name, f"checkpoint_last.pth"), to_save_state_refine)

    # Extract warped cloth images at the end of training
    fabric.print("Extracting warped cloth images...")
    # extraction_dataset_paired = torch.utils.data.ConcatDataset([dataset_test_paired, dataset_train])
    # extraction_dataloader_paired = DataLoader(batch_size=args.batch_size,
    #                                           dataset=extraction_dataset_paired,
    #                                           shuffle=False,
    #                                           num_workers=args.workers,
    #                                           drop_last=False)
    extraction_dataloader_paired = dataloader_test
    if args.save_path:
        warped_cloth_root = args.save_path
    else:
        warped_cloth_root = PROJECT_ROOT / 'data'

    save_name_paired = warped_cloth_root / 'warped_cloths' / args.dataset
    extract_images(extraction_dataloader_paired, tps, refinement, save_name_paired, args.height, args.width)

    # already combined all test datasets into one
    # extraction_dataset = dataset_test_unpaired
    # extraction_dataloader_paired = DataLoader(batch_size=args.batch_size,
    #                                           dataset=extraction_dataset,
    #                                           shuffle=False,
    #                                           num_workers=args.workers)
    #
    # save_name_unpaired = warped_cloth_root / 'warped_cloths_unpaired' / args.dataset
    # extract_images(extraction_dataloader_paired, tps, refinement, save_name_unpaired, args.height, args.width)


if __name__ == '__main__':
    logger = TensorBoardLogger(root_dir="./logs",
                               name="multi-train-combined-tps")
    fabric = Fabric(strategy='ddp', devices=3, precision='bf16-mixed', loggers=[logger])
    fabric.launch()
    main(logger, fabric)
