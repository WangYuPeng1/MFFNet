import argparse
import datetime
import numpy as np
import time
import torch
import torch.backends.cudnn as cudnn
import json
import os
from pathlib import Path
from timm.data.mixup import Mixup
from timm.loss import LabelSmoothingCrossEntropy, SoftTargetCrossEntropy
from timm.utils import ModelEma
from config.configs import get_args_parser
from models.miner import convnext_base
from torch.utils.data import DataLoader
from datasets_builder import build_dataset
from processor.engine import train_one_epoch, evaluate
from processor.optim_factory import create_optimizer, LayerDecayValueAssigner
from util.utils import NativeScalerWithGradNormCount as NativeScaler
from util import utils
from util.utils import create_logger, SoftCrossEntropyLoss
import torch.distributed as dist
import warnings

warnings.filterwarnings('ignore')


def main(args):
    # ---------------------- prepare running --------------------------------
    # GPU settings
    global model
    assert torch.cuda.is_available()
    os.environ['CUDA_VISIBLE_DEVICES'] = '0'
    device = torch.device("cuda")
    # logging file
    if args.eval is False:
        logger = create_logger(output_dir=args.output_dir)
    else:
        logger = None
    # fix the seed for reproducibility
    seed = args.seed
    torch.manual_seed(seed)
    np.random.seed(seed)
    cudnn.benchmark = True

    # ------------------------- build dataset ------------------------------
    dataset_train, args.nb_classes = build_dataset(is_train=True, args=args)
    # Disabling evaluation during training
    if args.disable_eval:
        args.dist_eval = False
        dataset_val = None
    else:
        dataset_val, _ = build_dataset(is_train=False, args=args)

    if args.log_dir is not None:  # 没用
        os.makedirs(args.log_dir, exist_ok=True)
        log_writer = utils.TensorboardLogger(log_dir=args.log_dir)
    else:
        log_writer = None

    if args.enable_wandb:  # 没用
        wandb_logger = utils.WandbLogger(args)
    else:
        wandb_logger = None

    data_loader_train = DataLoader(dataset_train, batch_size=args.batch_size, shuffle=True,
                                   num_workers=args.num_workers, pin_memory=args.pin_mem, drop_last=True)
    data_loader_val = DataLoader(dataset_val, batch_size=int(1.5 * args.batch_size), shuffle=False,
                                 num_workers=args.num_workers, pin_memory=args.pin_mem, drop_last=True)

    # ------------------------- mixup setting ------------------------------
    # mixup_fn = None
    # mixup_active = args.mixup > 0 or args.cutmix > 0. or args.cutmix_minmax is not None
    # if mixup_active:
    #     logger.info("Mixup is activated!")
    #     mixup_fn = Mixup(
    #         mixup_alpha=args.mixup, cutmix_alpha=args.cutmix, cutmix_minmax=args.cutmix_minmax,
    #         prob=args.mixup_prob, switch_prob=args.mixup_switch_prob, mode=args.mixup_mode,
    #         label_smoothing=args.smoothing, num_classes=args.nb_classes)

    # ---------------------- initialize the model ------------------------------
    if args.model == 'convnext_base':
        model = convnext_base(
            pretrained=False,
            M=args.attentions,
            num_classes=args.nb_classes,
            drop_path_rate=args.drop_path,
            layer_scale_init_value=args.layer_scale_init_value,
            head_init_scale=args.head_init_scale,
            use_mha=args.use_mha,
            use_ref=args.use_ref,
        )
        checkpoint = torch.load(args.finetune, map_location='cpu')
        checkpoint_model = None
        for model_key in args.model_key.split('|'):
            if model_key in checkpoint:
                checkpoint_model = checkpoint[model_key]
                logger.info("Load state_dict by model_key = %s" % model_key)
                break
        if checkpoint_model is None:
            checkpoint_model = checkpoint
        state_dict = model.state_dict()
        for k in ['head.weight', 'head.bias']:
            if k in checkpoint_model and checkpoint_model[k].shape != state_dict[k].shape:
                del checkpoint_model[k]
        utils.load_state_dict(model, checkpoint_model, prefix=args.model_prefix)
    else:
        ValueError("Unsupported model: %s" % args.model)

    model.to(device)
    model_ema = None
    # EMA滑动平均训练方式
    if args.model_ema:  # 没用
        # Important to create EMA model after cuda(), DP wrapper, and AMP but before SyncBN and DDP wrapper
        model_ema = ModelEma(
            model,
            decay=args.model_ema_decay,
            device='cpu' if args.model_ema_force_cpu else '',
            resume='')
        logger.info("Using EMA with decay = %.8f" % args.model_ema_decay)

    model_without_ddp = model
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)

    # total_batch_size = args.batch_size * args.update_freq * utils.get_world_size()
    total_batch_size = args.batch_size * args.update_freq
    num_training_steps_per_epoch = len(dataset_train) // total_batch_size

    if args.layer_decay < 1.0 or args.layer_decay > 1.0:
        num_layers = 12  # convnext layers divided into 12 parts, each with a different decayed lr value.
        if args.model in ['convnext_small', 'convnext_base', 'convnext_large', 'convnext_xlarge']:
            assigner = LayerDecayValueAssigner(
                list(args.layer_decay ** (num_layers + 1 - i) for i in range(num_layers + 2)))

    # model distributed
    # if args.distributed:  # 没用
    #     model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu], broadcast_buffers=False,
    #                                                       find_unused_parameters=True)
    #     model_without_ddp = model.module

    # -------------------- initialize the optimizer ------------------------------
    optimizer = create_optimizer(
        args, model_without_ddp, skip_list=None,
        get_num_layer=assigner.get_layer_id if assigner is not None else None,
        get_layer_scale=assigner.get_scale if assigner is not None else None)

    loss_scaler = NativeScaler()  # if args.use_amp is False, this won't be used  False
    # schedule
    logger.info("Use Cosine LR scheduler")
    lr_schedule_values = utils.cosine_scheduler(
        args.lr, args.min_lr, args.epochs, num_training_steps_per_epoch,
        warmup_epochs=args.warmup_epochs, warmup_steps=args.warmup_steps,
    )
    # weight decay
    if args.weight_decay_end is None:
        args.weight_decay_end = args.weight_decay
    wd_schedule_values = utils.cosine_scheduler(
        args.weight_decay, args.weight_decay_end, args.epochs, num_training_steps_per_epoch)

    if mixup_fn is not None:
        # smoothing is handled with mixup label transform
        criterion = SoftTargetCrossEntropy()
    elif args.smoothing > 0.:
        criterion = LabelSmoothingCrossEntropy(smoothing=args.smoothing)
    else:
        criterion = SoftCrossEntropyLoss(gama=args.novel_loss)

    logger.info("Criterion: %s" % str(criterion))

    utils.auto_load_model(
        args=args, model=model, model_without_ddp=model_without_ddp,
        optimizer=optimizer, loss_scaler=loss_scaler, model_ema=model_ema)
    # for evaluation, Perform evaluation only
    if args.eval:
        logger.info(f"Eval only mode")
        ckpt = torch.load("checkpoint/" + args.dataset + ".pth")["model"]
        model_dict = model.state_dict()
        ckpt = {k: v for k, v in ckpt.items() if k in model_dict}
        model.load_state_dict(ckpt)
        test_stats = evaluate(data_loader_val, model, device, use_amp=args.use_amp, logger=logger,
                              update_freq=args.update_freq)
        logger.info(f"Accuracy of the network on {len(dataset_val)} test images: {test_stats['acc1']:.5f}%")
        return

    # ------------------------- training stage ------------------------------
    max_accuracy = 0.0
    if args.model_ema and args.model_ema_eval:
        max_accuracy_ema = 0.0
    logger.info("Start training")
    start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        if log_writer is not None:  # Tensorboard
            log_writer.set_step(epoch * num_training_steps_per_epoch * args.update_freq)
        if wandb_logger:  # wandb
            wandb_logger.set_steps()
        # training
        train_stats = train_one_epoch(
            model, criterion, data_loader_train, optimizer,
            device, epoch, loss_scaler, args.clip_grad, model_ema, mixup_fn,
            log_writer=log_writer, wandb_logger=wandb_logger, start_steps=epoch * num_training_steps_per_epoch,
            lr_schedule_values=lr_schedule_values, wd_schedule_values=wd_schedule_values,
            num_training_steps_per_epoch=num_training_steps_per_epoch, update_freq=args.update_freq,
            use_amp=args.use_amp, logger=logger
        )
        # save params
        if args.output_dir and args.save_ckpt:
            if (epoch + 1) % args.save_ckpt_freq == 0 or epoch + 1 == args.epochs:
                utils.save_model(
                    args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
                    loss_scaler=loss_scaler, epoch=epoch, model_ema=model_ema)

        # evaluate
        if data_loader_val is not None:
            test_stats = evaluate(data_loader_val, model, device, use_amp=args.use_amp, logger=logger,
                                  update_freq=args.update_freq)
            logger.info(f"test accuracy : {test_stats['acc1']:.1f}%")
            if max_accuracy < test_stats["acc1"]:
                max_accuracy = test_stats["acc1"]
                if wandb_logger is not None:
                    wandb.run.summary["Best Accuracy"] = max_accuracy
                    wandb.run.summary["Best Epoch"] = epoch
                if args.output_dir and args.save_ckpt:
                    utils.save_model(
                        args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
                        loss_scaler=loss_scaler, epoch="best", model_ema=model_ema)
            acc1 = test_stats["acc1"]
            logger.info(f"Accuracy of the network on the {len(dataset_val)} test images: {acc1:.1f}%")
            logger.info(f'Max accuracy: {max_accuracy:.2f}%')

            if log_writer is not None:
                log_writer.update(test_acc1=test_stats['acc1'], head="perf", step=epoch)
                log_writer.update(test_acc5=test_stats['acc5'], head="perf", step=epoch)
                log_writer.update(test_loss=test_stats['loss'], head="perf", step=epoch)

            log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                         **{f'test_{k}': v for k, v in test_stats.items()},
                         'epoch': epoch,
                         'n_parameters': n_parameters}

            # repeat testing routines for EMA, if ema eval is turned on
            if args.model_ema and args.model_ema_eval:
                test_stats_ema = evaluate(data_loader_val, model_ema.ema, device, use_amp=args.use_amp, logger=logger,
                                          update_freq=args.update_freq)
                # logger.info(f"Accuracy of the model EMA on {len(dataset_val)} test images: {test_stats_ema[
                # 'acc1']:.1f}%")
                if max_accuracy_ema < test_stats_ema["acc1"]:
                    max_accuracy_ema = test_stats_ema["acc1"]
                    if args.output_dir and args.save_ckpt:
                        utils.save_model(
                            args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
                            loss_scaler=loss_scaler, epoch="best-ema", model_ema=model_ema)
                    logger.info(f'Max EMA accuracy: {max_accuracy_ema:.2f}%')
                if log_writer is not None:
                    log_writer.update(test_acc1_ema=test_stats_ema['acc1'], head="perf", step=epoch)
                log_stats.update({**{f'test_{k}_ema': v for k, v in test_stats_ema.items()}})
        else:
            log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                         'epoch': epoch,
                         'n_parameters': n_parameters}
        # update logger info
        if args.output_dir:
            if log_writer is not None:
                log_writer.flush()
            with open(os.path.join(args.output_dir, "record.txt"), mode="a", encoding="utf-8") as f:
                f.write(json.dumps(log_stats) + "\n")

        if wandb_logger:
            wandb_logger.log_epoch_metrics(log_stats)

    if wandb_logger and args.wandb_ckpt and args.save_ckpt and args.output_dir:
        wandb_logger.log_checkpoints()

    # ------------------------- finished ------------------------------
    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    logger.info('Training time {}'.format(total_time_str))


if __name__ == '__main__':

    parser = argparse.ArgumentParser('ConvNeXt training and evaluation script', parents=[get_args_parser()])
    args = parser.parse_args()

    args.output_dir = os.path.join(args.output_dir, '%s_%s' % (args.dataset, args.tag))

    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    with open(os.path.join(args.output_dir, 'config.txt'), 'w') as f:
        argsDict = args.__dict__
        f.writelines('------------------ start ------------------' + '\n')
        for eachArg, value in argsDict.items():
            f.writelines(eachArg + ' : ' + str(value) + '\n')
        f.writelines('------------------- end -------------------')

    main(args)
