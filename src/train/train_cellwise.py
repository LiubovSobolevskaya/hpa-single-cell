# coding: utf-8
import sys
sys.path.insert(0, '..')
import argparse
import shutil
import pickle

import torch
import torch.optim
from torch.utils.data import DataLoader
from torch.utils.data.sampler import RandomSampler, SequentialSampler
from torch.nn import DataParallel
from torch.backends import cudnn
import torch.nn.functional as F
from torch.autograd import Variable
from sklearn.metrics import average_precision_score
import pandas as pd

from ..data.augment_util_bestfitting import train_multi_augment2
from ..models.layers_bestfitting.loss import *
from ..models.layers_bestfitting.scheduler import *
from ..models.networks_bestfitting.imageclsnet import init_network
from ..data.datasets import ProteinDatasetCellSeparateLoading #ProteinDatasetCellLevel
from ..data.utils import get_train_df_ohe, get_public_df_ohe, get_class_names
from src.commons.utils import Logger
import multiprocessing
import time

loss_names = ['FocalSymmetricLovaszHardLogLoss']

parser = argparse.ArgumentParser(description='PyTorch Protein Classification')
parser.add_argument('--out_dir', default='densenet121_1024_all_data_obvious_neg', type=str, help='destination where trained network should be saved')
parser.add_argument('--gpu-id', default='0', type=str, help='gpu id used for training (default: 0)')
parser.add_argument('--arch', default='class_densenet121_large_dropout', type=str,
                    help='model architecture (default: class_densenet121_large_dropout)')
parser.add_argument('--effnet-encoder', default='efficientnet-b0', type=str)

parser.add_argument('--num_classes', default=19, type=int, help='number of classes (default: 19)')
parser.add_argument('--in_channels', default=4, type=int, help='in channels (default: 4)')
parser.add_argument('--loss', default='FocalSymmetricLovaszHardLogLoss', choices=loss_names, type=str,
                    help='loss function: ' + ' | '.join(loss_names) + ' (deafault: FocalSymmetricLovaszHardLogLoss)')
parser.add_argument('--scheduler', default='Adam20WarmUP', type=str, help='scheduler name')
parser.add_argument('--scheduler-lr-multiplier', default=1.0, type=float, help='scheduler lr multiplier')
parser.add_argument('--scheduler-epoch-offset', default=0, type=int, help='epoch offset for the scheduler')
parser.add_argument('--epochs', default=10, type=int, help='number of total epochs to run (default: 55)')
parser.add_argument('--img_size', default=512, type=int, help='image size (default: 512)')
parser.add_argument('--batch_size', default=32, type=int, help='train mini-batch size (default: 32)')
parser.add_argument('--workers', default=multiprocessing.cpu_count() - 1, type=int, help='number of data loading workers (default: 3)')
parser.add_argument('--fold', default=0, type=int, help='index of fold (default: 0)')
parser.add_argument('--clipnorm', default=1, type=int, help='clip grad norm')
parser.add_argument('--resume', default=None, type=str, help='name of the latest checkpoint (default: None)')
parser.add_argument('--load-state-dict-path', default=None, type=str, help='path to .h5 file with a state-dict to load before training (default: None)')
parser.add_argument('--cell-level-labels-path', default='../output/image_level_labels.h5', type=str)
parser.add_argument('--eval-at-start', action='store_true')
parser.add_argument('--image-level-labels', action='store_true')
parser.add_argument('--normalize', action='store_true')

parser.add_argument('--gradient-accumulation-steps', default=50, type=int)


def main():
    args = parser.parse_args()

    log_out_dir = os.path.join(RESULT_DIR, 'logs', args.out_dir, 'fold%d' % args.fold)
    if not os.path.exists(log_out_dir):
        os.makedirs(log_out_dir)
    log = Logger()
    log.open(os.path.join(log_out_dir, 'log.train.txt'), mode='a')

    model_out_dir = os.path.join(RESULT_DIR, 'models', args.out_dir, 'fold%d' % args.fold)
    log.write(">> Creating directory if it does not exist:\n>> '{}'\n".format(model_out_dir))
    if not os.path.exists(model_out_dir):
        os.makedirs(model_out_dir)

    # set cuda visible device
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_id
    cudnn.benchmark = True
    # cudnn.enabled = False

    # set random seeds
    torch.manual_seed(0)
    torch.cuda.manual_seed_all(0)
    np.random.seed(0)

    model_params = {}
    model_params['architecture'] = args.arch
    model_params['num_classes'] = args.num_classes
    model_params['in_channels'] = args.in_channels
    if 'efficientnet' in args.arch:
        model_params['image_size'] = args.img_size
        model_params['encoder'] = args.effnet_encoder

    model = init_network(model_params)

    if args.load_state_dict_path is not None:
        if args.load_state_dict_path == 'use-img-level-densenet-ckpt':
            model_dir = '../output/models/densenet121_1024_all_data__obvious_neg__gradaccum_20__start_lr_3e6'
            pretrained_ckpt_path = os.path.join(f'{model_dir}', f'fold{args.fold}', 'final.pth')
        else:
            pretrained_ckpt_path = args.load_state_dict_path
        init_pretrained = torch.load(pretrained_ckpt_path)
        model.load_state_dict(init_pretrained['state_dict'])

    model.cuda()

    # define loss function (criterion)
    try:
        criterion = eval(args.loss)().cuda()
    except:
        raise(RuntimeError("Loss {} not available!".format(args.loss)))

    start_epoch = 0
    best_loss = 1e5
    best_epoch = 0
    best_focal = float('inf')

    # define scheduler
    try:
        scheduler = eval(args.scheduler)(scheduler_lr_multiplier=args.scheduler_lr_multiplier,
                                         scheduler_epoch_offset=args.scheduler_epoch_offset)
    except:
        raise (RuntimeError("Scheduler {} not available!".format(args.scheduler)))
    optimizer = scheduler.schedule(model, start_epoch, args.epochs)[0]

    # optionally resume from a checkpoint
    if args.resume:
        args.resume = os.path.join(model_out_dir, args.resume)
        if os.path.isfile(args.resume):
            # load checkpoint weights and update model and optimizer
            log.write(">> Loading checkpoint:\n>> '{}'\n".format(args.resume))

            checkpoint = torch.load(args.resume)
            start_epoch = checkpoint['epoch']
            best_epoch = checkpoint['best_epoch']
            best_focal = checkpoint['best_map']
            model.load_state_dict(checkpoint['state_dict'])

            optimizer_fpath = args.resume.replace('.pth', '_optim.pth')
            if os.path.exists(optimizer_fpath):
                log.write(">> Loading checkpoint:\n>> '{}'\n".format(optimizer_fpath))
                optimizer.load_state_dict(torch.load(optimizer_fpath)['optimizer'])
            log.write(">>>> loaded checkpoint:\n>>>> '{}' (epoch {})\n".format(args.resume, checkpoint['epoch']))
        else:
            log.write(">> No checkpoint found at '{}'\n".format(args.resume))

    # Data loading code
    train_transform = train_multi_augment2

    with open('../input/imagelevel_folds_obvious_staining_5.pkl', 'rb') as f:
        folds = pickle.load(f)
    fold = args.fold
    trn_img_paths, val_img_paths = folds[fold]

    train_df = get_train_df_ohe(clean_from_duplicates=True)
    basepath_2_ohe_vector = {img: vec for img, vec in zip(train_df['img_base_path'], train_df.iloc[:, 2:].values)}

    public_hpa_df_17 = get_public_df_ohe(clean_from_duplicates=True)
    public_basepath_2_ohe_vector = {img_path: vec for img_path, vec in zip(public_hpa_df_17['img_base_path'],
                                                                           public_hpa_df_17.iloc[:, 2:].values)}
    basepath_2_ohe_vector.update(public_basepath_2_ohe_vector)

    available_paths = set(np.concatenate((train_df['img_base_path'].values, public_hpa_df_17['img_base_path'].values)))
    trn_img_paths = [path for path in trn_img_paths if path in available_paths]
    val_img_paths = [path for path in val_img_paths if path in available_paths]
    labels_df = pd.read_hdf(args.cell_level_labels_path)

    train_dataset = ProteinDatasetCellSeparateLoading(trn_img_paths,
                                            labels_df=labels_df,
                                            img_size=args.img_size,
                                            in_channels=args.in_channels,
                                            transform=train_transform,
                                                      int_labels=True,
                                                      image_level_labels=args.image_level_labels,
                                                      basepath_2_ohe=basepath_2_ohe_vector,
                                                      normalize=args.normalize
    )
    train_loader = DataLoader(
        train_dataset,
        sampler=RandomSampler(train_dataset),
        batch_size=args.batch_size,
        drop_last=False,
        num_workers=args.workers,
        pin_memory=True,
    )

    # valid_dataset = ProteinDatasetCellLevel(val_img_paths,
    #                                         labels_df=labels_df,
    #                                         img_size=args.img_size,
    #                                         batch_size=64,
    #                                         is_trainset=True,
    #                                         in_channels=args.in_channels)

    valid_dataset = ProteinDatasetCellSeparateLoading(val_img_paths,
                                            labels_df=labels_df,
                                            img_size=args.img_size,
                                            in_channels=args.in_channels,
                                                      int_labels=True,
                                                      image_level_labels=args.image_level_labels,
                                                      basepath_2_ohe=basepath_2_ohe_vector,
                                                      normalize=args.normalize)
    valid_loader = DataLoader(
        valid_dataset,
        sampler=SequentialSampler(valid_dataset),
        batch_size=args.batch_size,
        drop_last=False,
        num_workers=args.workers,
        pin_memory=True
    )

    log.write('** start training here! **\n')
    log.write('\n')
    log.write('epoch    iter      rate     |  train_loss/acc  |    valid_loss/acc/map/focal     |best_epoch/best_focal|  min \n')
    log.write('-----------------------------------------------------------------------------------------------------------------\n')
    start_epoch += 1

    if args.eval_at_start:
        with torch.no_grad():
            valid_loss, valid_acc, val_focal, val_map_score = validate(valid_loader, model, criterion, -1, log)
        print('\r', end='', flush=True)
        log.write(
            '%5.1f   %5d    %0.6f   |  %0.4f  %0.4f  |    %0.4f  %6.4f %6.4f %6.1f  |    %6.4f  %6.4f   | %3.1f min \n' % \
            (-1, -1, -1, -1, -1, valid_loss, valid_acc, val_map_score, val_focal,
                   best_epoch, best_focal, -1))

    for epoch in range(start_epoch, args.epochs + 1):
        end = time.time()

        # set manual seeds per epoch
        np.random.seed(epoch)
        torch.manual_seed(epoch)
        torch.cuda.manual_seed_all(epoch)

        # adjust learning rate for each epoch
        lr_list = scheduler.step(model, epoch, args.epochs)
        lr = lr_list[0]

        # train for one epoch on train set
        iter, train_loss, train_acc = train(train_loader, model, criterion, optimizer, epoch, clipnorm=args.clipnorm,
                                            lr=lr, agg_steps=args.gradient_accumulation_steps)

        with torch.no_grad():
            valid_loss, valid_acc, val_focal, val_map_score = validate(valid_loader, model, criterion, epoch, log)

        # remember best loss and save checkpoint
        is_best = val_focal < best_focal
        best_loss = min(valid_loss, best_loss)
        best_epoch = epoch if is_best else best_epoch
        best_focal = val_focal if is_best else best_focal

        print('\r', end='', flush=True)
        log.write('%5.1f   %5d    %0.6f   |  %0.4f  %0.4f  |    %0.4f  %6.4f %6.4f  %6.1f |  %6.4f  %6.4f | %3.1f min \n' % \
                  (epoch, iter + 1, lr, train_loss, train_acc, valid_loss, valid_acc, val_map_score, val_focal,
                   best_epoch, best_focal, (time.time() - end) / 60))

        save_model(model, is_best, model_out_dir, optimizer=optimizer, epoch=epoch, best_epoch=best_epoch, best_map=best_focal)


def train(train_loader, model, criterion, optimizer, epoch, clipnorm=1, lr=1e-5, agg_steps=1):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    accuracy = AverageMeter()

    # switch to train mode
    model.train()

    num_its = len(train_loader)
    end = time.time()
    iter = 0
    print_freq = 1
    optimizer.zero_grad()
    for iter, iter_data in enumerate(train_loader, 0):
        # measure data loading time
        data_time.update(time.time() - end)

        images, labels, indices = iter_data

        images = Variable(images.cuda())
        labels = Variable(labels.cuda())

        outputs = model(images)
        loss = criterion(outputs, labels, epoch=epoch)

        losses.update(loss.item())
        loss.backward()

        if iter % agg_steps == 0:
            torch.nn.utils.clip_grad_norm(model.parameters(), clipnorm)
            optimizer.step()
            # zero out gradients so we can accumulate new ones over batches
            optimizer.zero_grad()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        logits = outputs
        probs = F.sigmoid(logits)
        acc = multi_class_acc(probs, labels)
        accuracy.update(acc.item())

        if (iter + 1) % print_freq == 0 or iter == 0 or (iter + 1) == num_its:
            print('\r%5.1f   %5d    %0.6f   |  %0.4f  %0.4f  | ... ' % \
                  (epoch - 1 + (iter + 1) / num_its, iter + 1, lr, losses.avg, accuracy.avg),
                  end='', flush=True)

    return iter, losses.avg, accuracy.avg


def validate(valid_loader, model, criterion, epoch, log, focal_loss=FocalLoss().cuda()):
    batch_time = AverageMeter()
    losses = AverageMeter()
    accuracy = AverageMeter()

    # switch to evaluate mode
    model.eval()

    probs_list = []
    labels_list = []
    logits_list = []

    end = time.time()
    for it, iter_data in enumerate(valid_loader, 0):
        images, labels, indices = iter_data
        images = Variable(images.cuda())
        labels = Variable(labels.cuda())

        outputs = model(images)
        loss = criterion(outputs, labels, epoch=epoch)

        logits = outputs
        probs = F.sigmoid(logits)

        if np.random.rand() < 0.005:
            for prob, label in zip(probs, labels):
                if np.random.rand() < 0.001:
                    print('printing pred vs. label')
                    for p, l in zip(prob.cpu().detach().numpy(), label.cpu().detach().numpy()):
                        print(p, l)
                    print('-' * 30)

        acc = multi_class_acc(probs, labels)

        probs_list.append(probs.cpu().detach().numpy())
        labels_list.append(labels.cpu().detach().numpy())
        logits_list.append(logits.cpu().detach().numpy())

        losses.update(loss.item())
        accuracy.update(acc.item())

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

    probs = np.vstack(probs_list)
    y_true = np.vstack(labels_list)

    logits = np.vstack(logits_list)
    valid_focal_loss = focal_loss.forward(torch.from_numpy(logits), torch.from_numpy(y_true))

    class_names = get_class_names()
    map_scores = average_precision_score(y_true, probs, average=None)
    for class_name, map_score in zip(class_names, map_scores):
        log.write(f'{class_name}: {map_score:.2f}\n')

    return losses.avg, accuracy.avg, valid_focal_loss, np.nanmean(map_scores)


def save_model(model, is_best, model_out_dir, optimizer=None, epoch=None, best_epoch=None, best_map=None):
    if type(model) == DataParallel:
        state_dict = model.module.state_dict()
    else:
        state_dict = model.state_dict()
    for key in state_dict.keys():
        state_dict[key] = state_dict[key].cpu()

    model_fpath = os.path.join(model_out_dir, '%03d.pth' % epoch)
    torch.save({
        'save_dir': model_out_dir,
        'state_dict': state_dict,
        'best_epoch': best_epoch,
        'epoch': epoch,
        'best_map': best_map,
    }, model_fpath)

    optim_fpath = os.path.join(model_out_dir, '%03d_optim.pth' % epoch)
    if optimizer is not None:
        torch.save({
            'optimizer': optimizer.state_dict(),
        }, optim_fpath)

    if is_best:
        best_model_fpath = os.path.join(model_out_dir, 'final.pth')
        shutil.copyfile(model_fpath, best_model_fpath)
        if optimizer is not None:
            best_optim_fpath = os.path.join(model_out_dir, 'final_optim.pth')
            shutil.copyfile(optim_fpath, best_optim_fpath)

def multi_class_acc(preds, targs, th=0.5):
    preds = (preds > th).int()
    return (preds == targs).float().mean()

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0.
        self.avg = 0.
        self.sum = 0.
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

if __name__ == '__main__':
    print('%s: calling main function ... \n' % os.path.basename(__file__))
    main()
    print('\nsuccess!')
