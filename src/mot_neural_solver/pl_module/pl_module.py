import os
import os.path as osp

import pandas as pd

from torch_geometric.data import DataLoader

import torch

from torch import optim as optim_module
from torch.optim import lr_scheduler as lr_sched_module
from torch.nn import functional as F

import pytorch_lightning as pl

from mot_neural_solver.data.mot_graph_dataset import MOTGraphDataset
from mot_neural_solver.models.mpn import MOTMPNet
from mot_neural_solver.models.resnet import resnet50_fc256, load_pretrained_weights
from mot_neural_solver.path_cfg import OUTPUT_PATH
from mot_neural_solver.utils.evaluation import compute_perform_metrics
from mot_neural_solver.tracker.mpn_tracker import MPNTracker

from PIL import Image
import heapq
import random
import numpy as np
from matplotlib.pyplot import imshow


class MOTNeuralSolver(pl.LightningModule):
    """
    Pytorch Lightning wrapper around the MPN defined in model/mpn.py.
    (see https://pytorch-lightning.readthedocs.io/en/latest/lightning-module.html)

    It includes all data loading and train / val logic., and it is used for both training and testing models.
    """

    def __init__(self, hparams):
        super().__init__()

        self.hparams = hparams
        self.model, self.cnn_model = self.load_model()
        self.right_num = 0
        self.total_num = 0
        self.i = 0

    def forward(self, x):
        self.model(x)

    def load_model(self):
        cnn_arch = self.hparams['graph_model_params']['cnn_params']['arch']
        if torch.cuda.is_available():
            model = MOTMPNet(self.hparams['graph_model_params']).cuda()
            cnn_model = resnet50_fc256(10, loss='xent', pretrained=True).cuda()
        else:
            model = MOTMPNet(self.hparams['graph_model_params'])
            cnn_model = resnet50_fc256(10, loss='xent', pretrained=True)

        load_pretrained_weights(cnn_model,
                                osp.join(OUTPUT_PATH,
                                         self.hparams['graph_model_params']['cnn_params']['model_weights_path'][
                                             cnn_arch]))
        cnn_model.return_embeddings = True

        return model, cnn_model

    def _get_data(self, mode, return_data_loader=True):
        assert mode in ('train', 'val', 'test')

        dataset = MOTGraphDataset(dataset_params=self.hparams['dataset_params'],
                                  mode=mode,
                                  cnn_model=self.cnn_model,
                                  splits=self.hparams['data_splits'][mode],
                                  logger=None)
        if mode == 'train':
            self.dataset = dataset

        if return_data_loader and len(dataset) > 0:
            train_dataloader = DataLoader(dataset,
                                          batch_size=self.hparams['train_params']['batch_size'],
                                          shuffle=True if mode == 'train' else False,
                                          num_workers=self.hparams['train_params']['num_workers'])
            return train_dataloader

        elif return_data_loader and len(dataset) == 0:
            return []

        else:
            return dataset

    def train_dataloader(self):
        return self._get_data(mode='train')

    def val_dataloader(self):
        return self._get_data('val')

    def test_dataset(self, return_data_loader=False):
        return self._get_data('test', return_data_loader=return_data_loader)

    def configure_optimizers(self):
        optim_class = getattr(optim_module, self.hparams['train_params']['optimizer']['type'])
        optimizer = optim_class(self.model.parameters(), **self.hparams['train_params']['optimizer']['args'])

        if self.hparams['train_params']['lr_scheduler']['type'] is not None:
            lr_sched_class = getattr(lr_sched_module, self.hparams['train_params']['lr_scheduler']['type'])
            lr_scheduler = lr_sched_class(optimizer, **self.hparams['train_params']['lr_scheduler']['args'])

            return [optimizer], [lr_scheduler]

        else:
            return optimizer

    def _compute_loss(self, outputs, batch, train_val):
        # Define Balancing weight
        positive_vals = batch.edge_labels.sum()

        use_attention = self.hparams['graph_model_params']['attention']['use_attention']
        use_supervision = self.hparams['graph_model_params']['attention']['use_supervision']
        if positive_vals:
            pos_weight = (batch.edge_labels.shape[0] - positive_vals) / positive_vals

        else:  # If there are no positives labels, avoid dividing by zero
            pos_weight = 0

        # Compute Weighted BCE:
        loss_class = 0
        num_steps_class = len(outputs['classified_edges'])

        for step in range(num_steps_class):
            loss_class += F.binary_cross_entropy_with_logits(outputs['classified_edges'][step].view(-1),
                                                             batch.edge_labels.view(-1),
                                                             pos_weight=pos_weight)

        # print(batch.ix[0])
        if train_val == 'train':
            self.i += 1

        if train_val == 'train' and self.i % 748 == 0:
            print("YES")
            k0 = 4
            row, col = batch.edge_index
            row_list = row.tolist()
            row_uniq = set(row_list)
            stat = [row_list.count(x) for x in row_uniq]

            valid_index = np.where(np.array(stat) > 10)[0].tolist()
            choices_index = random.choices(valid_index, k=k0)

            choices_part = [0] * k0

            matches = []
            for i, j in enumerate(choices_index):
                identifer = np.array([0] * len(row_list))
                matches += [np.where(np.array(row_list) == j)[0]]
                identifer[matches[i]] = np.array([1] * stat[j])
                choices_part[i] = identifer

            count = [0] * 8
            MOTGraph = []
            for i in range(8):
                MOTGraph += [self.dataset.get(batch.ix[i].cpu().item())]
                count[i] = len(MOTGraph[i].graph_df)

            accm = np.cumsum(count)
            interval = [0] + accm.tolist()

            graph_index = [-1] * k0
            local_base = [-1] * k0
            for k in range(k0):
                for i in range(1, 9):
                    if choices_index[k] < interval[i]:
                        graph_index[k] = i - 1
                        local_base[k] = interval[i - 1]
                        break

            a = outputs['att_coefficients'][-1].squeeze(0)
            for k in range(k0):
                b = a.detach().cpu()
                b = b[matches[k]].tolist()
                index_largest = heapq.nlargest(3, range(len(b)), key=b.__getitem__)
                index_smallest = heapq.nsmallest(3, range(len(b)), key=b.__getitem__)

                tuple_det = MOTGraph[graph_index[k]].graph_df.iloc[choices_index[k] - local_base[k]]
                path = tuple_det["frame_path"]

                im = Image.open(path)
                (left, upper, right, lower) = (tuple_det["bb_left"], tuple_det["bb_top"]
                                               , tuple_det["bb_right"], tuple_det["bb_bot"])

                # Here the image "im" is cropped and assigned to new variable im_crop
                im_crop = im.crop((left, upper, right, lower))
                filename = "/content/ADL4CV/image/" + str(self.i // 748) + "_pic" + str(k) + "_original.jpg"
                im_crop.save(filename)

                for key, v in enumerate(index_largest):
                    id0 = col[matches[k][v]].cpu().item()

                    tuple_det = MOTGraph[graph_index[k]].graph_df.iloc[id0 - local_base[k]]
                    path = tuple_det["frame_path"]

                    im = Image.open(path)
                    (left, upper, right, lower) = (tuple_det["bb_left"], tuple_det["bb_top"]
                                                   , tuple_det["bb_right"], tuple_det["bb_bot"])

                    # Here the image "im" is cropped and assigned to new variable im_crop
                    im_crop = im.crop((left, upper, right, lower))
                    filename = "/content/ADL4CV/image/" + str(self.i // 748) + "_pic" + str(k) + "_large" + str(
                        key) + ".jpg"
                    im_crop.save(filename)

                for key, v in enumerate(index_smallest):
                    id0 = col[matches[k][v]].cpu().item()

                    tuple_det = MOTGraph[graph_index[k]].graph_df.iloc[id0 - local_base[k]]
                    path = tuple_det["frame_path"]

                    im = Image.open(path)
                    (left, upper, right, lower) = (tuple_det["bb_left"], tuple_det["bb_top"]
                                                   , tuple_det["bb_right"], tuple_det["bb_bot"])

                    # Here the image "im" is cropped and assigned to new variable im_crop
                    im_crop = im.crop((left, upper, right, lower))
                    filename = "/content/ADL4CV/image/" + str(self.i // 748) + "_pic" + str(k) + "_small" + str(key) + ".jpg"
                    im_crop.save(filename)

        """
        total_edge = torch.sum(batch.edge_labels.view(-1)).cpu().item()
        valid_edge = torch.sum(outputs["illustrate"] * batch.edge_labels.view(-1).unsqueeze(-1),dim=0)
        print("\n")
        print(total_edge)
        print(valid_edge)
        print(total_edge/valid_edge)
        print("\n")
        """

        if not use_attention or not use_supervision:
            return loss_class

        #######################################
        # add supervision on attention factor #
        #######################################

        num_steps_attention = len(outputs['att_coefficients'])
        head_factor = self.hparams['graph_model_params']['attention']['attention_head_num']
        att_regu_strength = self.hparams['graph_model_params']['attention']['attention_supervision_strength']
        att_loss_matrix = torch.empty(size=(head_factor, num_steps_attention)).cuda()
        for step in range(num_steps_attention):
            for head in range(head_factor):
                att_loss_matrix[head, step] = F.binary_cross_entropy_with_logits(
                    outputs['att_coefficients'][step][head].view(-1),
                    batch.edge_labels.view(-1),
                    pos_weight=pos_weight)
        att_loss = torch.sum(att_loss_matrix) / head_factor
        return loss_class + att_regu_strength * att_loss

    def _train_val_step(self, batch, batch_idx, train_val):
        self.i += 1
        device = (next(self.model.parameters())).device
        batch.to(device)

        outputs = self.model(batch)
        loss = self._compute_loss(outputs, batch, train_val)
        logs = {**compute_perform_metrics(outputs, batch), **{'loss': loss}}
        log = {key + f'/{train_val}': val for key, val in logs.items()}

        if train_val == 'train':

            return {'loss': loss, 'log': log}

        else:
            return log

    def training_step(self, batch, batch_idx):
        return self._train_val_step(batch, batch_idx, 'train')

    def validation_step(self, batch, batch_idx):
        return self._train_val_step(batch, batch_idx, 'val')

    def validation_epoch_end(self, outputs):
        metrics = pd.DataFrame(outputs).mean(axis=0).to_dict()
        metrics = {metric_name: torch.as_tensor(metric) for metric_name, metric in metrics.items()}
        return {'val_loss': metrics['loss/val'], 'log': metrics}

    def track_all_seqs(self, output_files_dir, dataset, use_gt=False, verbose=False):
        tracker = MPNTracker(dataset=dataset,
                             graph_model=self.model,
                             use_gt=use_gt,
                             eval_params=self.hparams['eval_params'],
                             dataset_params=self.hparams['dataset_params'])

        constraint_sr = pd.Series(dtype=float)
        for seq_name in dataset.seq_names:
            print("Tracking", seq_name)
            if verbose:
                print("Tracking sequence ", seq_name)

            os.makedirs(output_files_dir, exist_ok=True)
            _, constraint_sr[seq_name] = tracker.track(seq_name,
                                                       output_path=osp.join(output_files_dir, seq_name + '.txt'))

            if verbose:
                print("Done! \n")

        constraint_sr['OVERALL'] = constraint_sr.mean()

        return constraint_sr