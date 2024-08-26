import os
import gc
import time
import shutil
import argparse
import yaml, json
import logging

from pathlib import Path
COMMON_ROOT = Path("/shared/common")
DATASET_ROOT = Path("/shared/datasets")
CORE_DIR = Path(__file__).resolve().parent.parent.parent # /source/autonn_core
CFG_PATH = CORE_DIR / 'tango' / 'common' / 'cfg'

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler

# from torch.utils.tensorboard import SummaryWriter
from tensorboardX import SummaryWriter

from . import status_update, Info
from .train import train
from .search import search
from .visualize import BasemodelViewer
from .export import export_weight, export_config
from tango.utils.general import (   increment_path,
                                    fitness,
                                    get_latest_run,
                                    check_file,
                                    print_mutation,
                                    set_logging,
                                    colorstr        )
from tango.utils.plots import plot_evolution
from tango.utils.wandb_logging.wandb_utils import check_wandb_resume


TASK_TO_MODEL_TABLE = {
    "detection7": "yolov7",
    "detection": "yolov9",
    "classification": "resnet",
    "classification-c": "resnetc",
}

MODEL_TO_SIZE_TABLE = {
    "yolov7": {
        "cloud": "-d6",
        "k8s": "-e6",
        "k8sjetsonnano": "-e6",
        "pcweb": "-w6",
        "pc": "-w6",
        "jetsonagxorin": "x",
        "jetsonagxxavier": "x",
        "jetsonnano": "x",
        "galaxys22": "-supernet",
        "odroidn2": "-tiny",
    },
    "yolov9": {
        "cloud": "-e",
        "k8s": "-c",
        "k8sjetsonnano": "-c",
        "pcweb": "-m",
        "pc": "-m",
        "jetsonagxorin": "-s",
        "jetsonagxxavier": "-s",
        "jetsonnano": "-s",
        "galaxys22": "-supernet",
        "odroidn2": "-t",
    },
    "resnet": {
        "cloud": "152",
        "k8s": "101",
        "k8sjetsonnano": "101",
        "pcweb": "50",
        "pc": "50",
        "jetsonagxorin": "34",
        "jetsonagxxavier": "34",
        "jetsonnano": "34",
        "galaxys22": "18",
        "odroidn2": "18",
    },
    "resnetc": {
        "cloud": "200",
        "k8s": "152",
        "k8sjetsonnano": "110",
        "pcweb": "56",
        "pc": "56",
        "jetsonagxorin": "44",
        "jetsonagxxavier": "44",
        "jetsonnano": "44",
        "galaxys22": "32",
        "odroidn2": "20",
    },
}

logger = logging.getLogger(__name__)


def get_user_requirements(userid, projid):
    """
    Get user requirements(dataset, project, hyperparameters, arguments)
    """
    # ----------------------------- project_info -------------------------------
    PROJ_PATH = COMMON_ROOT / userid / projid
    proj_yaml_path = PROJ_PATH / "project_info.yaml"
    with open(proj_yaml_path, "r") as f:
        proj_info_dict = yaml.safe_load(f)
    status_update(userid, projid,
                  update_id="project_info",
                  update_content=proj_info_dict)

    info = Info.objects.get(userid=userid, project_id=projid)
    target = proj_info_dict['target_info'].replace('-', '').replace('_', '').lower()
    info.target = target
    info.device = proj_info_dict['acc']
    info.dataset = proj_info_dict['dataset']
    info.task = proj_info_dict['task_type']
    info.status = "running"
    info.progress = "setting"
    info.model_viz = "not ready"
    info.save()

    # ----------------------------- dataset ------------------------------------
    dataset_on_proj = proj_info_dict["dataset"]
    if os.path.isdir(str(DATASET_ROOT / dataset_on_proj)):
        dataset_yaml_path = DATASET_ROOT / dataset_on_proj / "dataset.yaml"
    else:
        logger.warn(f"There is no {DATASET_ROOT}/{dataset_on_proj}. "
                    f"Instead embedded COCO128 dataset will be used.")
        dataset_on_proj = 'coco128'
        dataset_yaml_path = CORE_DIR / 'datasets' / 'coco128' / 'dataset.yaml'
    with open(dataset_yaml_path) as f:
        data_dict = yaml.load(f, Loader=yaml.SafeLoader)
    data_dict['dataset_name'] = dataset_on_proj

    # ---------------------------- basemodel -----------------------------------
    basemodel_yaml_path, basemodel = base_model_select(userid,
                                                       projid,
                                                       proj_info_dict,
                                                       data_dict)
    with open(basemodel_yaml_path, "r") as f:
        basemodel_dict = yaml.load(f, Loader=yaml.SafeLoader)
    basemodel_dict['hyp'] = 'p5' if basemodel_dict['hyp'] == 'tiny' \
                                 else basemodel_dict['hyp']
    proj_info_dict['nas'] = True if basemodel['model_size'] == '-supernet' else False

    # --------------------------- hyperparameter -------------------------------
    hyp_yaml_path = CFG_PATH / f"hyp.scratch.{basemodel_dict['hyp']}.yaml"
    with open(hyp_yaml_path) as f:
        hyp_dict = yaml.safe_load(f)
    # hyp_dict['lrc'] = hyp_dict['lr0']
    hyp_dict['anchor_t'] = 5.0 # from yolov9
    status_update(userid, projid,
                  update_id="hyperparameter",
                  update_content=hyp_dict)

    # ---------------------------- arguments -----------------------------------
    task = proj_info_dict['task_type']
    # trick for nas (temp.)
    _task =  'detection7' if target == 'galaxys22' else task
    opt_yaml_path = CFG_PATH / f'args-{_task}.yaml'
    with open(opt_yaml_path, encoding='utf-8') as f:
        opt = argparse.Namespace(**yaml.safe_load(f))
    opt.project = str(PROJ_PATH)
    opt.data = str(dataset_yaml_path)
    opt.cfg = str(basemodel_yaml_path)
    opt.hyp = str(hyp_yaml_path)
    opt.img_size = [basemodel_dict['imgsz'], basemodel_dict['imgsz']]
    '''
    WORLD_SIZE: the total number of processes participating in the traininig 
                (typically the number of GPUs across all nodes)
    RANK:       global rank, the unique ID for each process involved in the training
                (ranging from 0 to WORLD_SIZE-1)
    LOCAL_RANK: the ID under the same node
                (often GPU is specified with LOCAL_RANK, but not necessarily 1-to-1)  
    '''
    opt.world_size = int(os.environ['WORLD_SIZE']) if 'WORLD_SIZE' in os.environ else 1
    opt.global_rank = int(os.environ['RANK']) if 'RANK' in os.environ else -1
    opt_content = vars(opt)
    status_update(userid, projid,
                  update_id="arguments",
                  update_content=opt_content)

    status_update(userid, projid,
                  update_id="basemodel",
                  update_content=basemodel)

    return proj_info_dict, opt, hyp_dict, basemodel_dict, data_dict


def base_model_select(userid, project_id, proj_info, data, manual_select=False):
    # read condition
    task = proj_info['task_type']
    target = proj_info['target_info'].replace('-', '').replace('_', '').lower()

    # trick for compact version of resnet
    task_ = 'classification-c' if data['nc'] <= 10 and task == 'classification' else task

    # trick for nas (temp.)
    if target == 'galaxys22':
        task_ = 'detection7'

    # look up table
    if not manual_select:
        model = TASK_TO_MODEL_TABLE[task_]
        size = MODEL_TO_SIZE_TABLE[model][target]
    else:
        model = manual_select['model']
        size = manual_select['size']

    # trick for compact version of resnet
    dirname = model if task_ != 'classification-c' else model[:-1] # remove 'c' from 'resnetc'
    filename = f'{model}{size}.yaml'

    # store basemodel.yaml
    PROJ_PATH = COMMON_ROOT / userid / project_id
    source_path = f'{CFG_PATH}/{dirname}/{filename}'
    target_path = f'{PROJ_PATH}/basemodel.yaml'
    shutil.copy(source_path, target_path)

    # construct nodes and edges
    viewer = BasemodelViewer(userid, project_id)
    viewer.parse_yaml(target_path, data)
    viewer.update()

    # save internally
    info = Info.objects.get(userid=userid, project_id=project_id)
    info.model_type = model
    info.model_size = size
    info.status = "running"
    info.progress = "bms"
    info.model_viz = "ready"
    info.save()

    # for updating status (P.M)
    model_p = model.upper()
    size_p = size.replace('-', '').replace('_', '').upper()
    basemodel = {
        "model_name": model_p,
        "model_size": size_p,
    }
    # print(basemodel)
    return target_path, basemodel


def run_autonn(userid, project_id, viz2code=False, nas=False, hpo=False):
    # Set Logging --------------------------------------------------------------
    set_logging(int(os.environ['RANK']) if 'RANK' in os.environ else -1)

    # Load settings ------------------------------------------------------------
    proj_info, opt, hyp, basemodel, data = get_user_requirements(userid, project_id)

    proj_info['userid'] = userid
    proj_info['project_id'] = project_id
    proj_info['nas'] = nas
    proj_info['viz'] = viz2code
    proj_info['hpo'] = hpo

    target = proj_info['target_info'] # PC, Galaxy_S22, etc.
    target_acc = proj_info['acc'] # cuda, opencl, cpu
    target_engine = proj_info['engine'].replace('-', '').replace('_', '').lower() # tensorrt, pytorch, tvm, etc.
    task = proj_info['task_type'] # detection, classification, llm


    # Clear CUDA memory --------------------------------------------------------
    torch.cuda.empty_cache()
    gc.collect()

    if basemodel['imgsz']==1280:
        return run_autonn_aux(proj_path, dataset_yaml_path, data, target, train_mode, final_arch)

    # wandb_run = check_wandb_resume(opt)
    # if opt.resume and not wandb_run:
    if opt.resume: # resume an interrupted run ---------------------------------
        ckpt = opt.resume if isinstance(opt.resume, str) else get_latest_run()  # specified or most recent path
        assert os.path.isfile(ckpt), 'ERROR: --resume checkpoint does not exist'
        apriori = opt.global_rank, opt.local_rank
        with open(Path(ckpt).parent.parent / 'opt.yaml') as f:
            opt = argparse.Namespace(**yaml.load(f, Loader=yaml.SafeLoader))  # replace
        opt.cfg, opt.weights, opt.resume, opt.batch_size, opt.global_rank, opt.local_rank = '', ckpt, True, opt.total_batch_size, *apriori  # reinstate
        logger.info('Resuming training from %s' % ckpt)
    else: # learn from scratch -------------------------------------------------
        # opt.hyp = opt.hyp or ('hyp.finetune.yaml' if opt.weights else 'hyp.scratch.yaml')
        opt.data, opt.cfg, opt.hyp = check_file(opt.data), check_file(opt.cfg), check_file(opt.hyp)  # check files
        assert len(opt.cfg) or len(opt.weights), 'either --cfg or --weights must be specified'
        opt.img_size.extend([opt.img_size[-1]] * (2 - len(opt.img_size)))  # extend to 2 sizes (train, test)
        opt.name = 'evolve' if opt.evolve else opt.name
        opt.save_dir = increment_path(Path(opt.project) / opt.name, exist_ok=opt.exist_ok | opt.evolve)  # increment run

    # Train --------------------------------------------------------------------
    if not opt.evolve:
        tb_writer = None  # init loggers
        # if opt.global_rank in [-1, 0]:
        #     prefix = colorstr('tensorboard: ')
        #     try:
        #         tb_writer = SummaryWriter(log_dir=str(opt.save_dir))  # Tensorboard
        #         logger.info(f"{prefix}Start with 'tensorboard --logdir {str(opt.save_dir)}', view at http://localhost:6006/")
        #     except Exception as e:
        #         logger.warn(f'{prefix}Fail to load tensorbord because {e}')
        #=======================================================================
        results, train_final = train(proj_info, hyp, opt, data, tb_writer)
        #=======================================================================
        # tb_writer.flush()

    # HPO ----------------------------------------------------------------------
    else:
        # Hyperparameter evolution metadata (mutation scale 0-1, lower_limit, upper_limit)
        meta = {'lr0': (1, 1e-5, 1e-1),  # initial learning rate (SGD=1E-2, Adam=1E-3)
                'lrf': (1, 0.01, 1.0),  # final OneCycleLR learning rate (lr0 * lrf)
                'momentum': (0.3, 0.6, 0.98),  # SGD momentum/Adam beta1
                'weight_decay': (1, 0.0, 0.001),  # optimizer weight decay
                'warmup_epochs': (1, 0.0, 5.0),  # warmup epochs (fractions ok)
                'warmup_momentum': (1, 0.0, 0.95),  # warmup initial momentum
                'warmup_bias_lr': (1, 0.0, 0.2),  # warmup initial bias lr
                'box': (1, 0.02, 0.2),  # box loss gain
                'cls': (1, 0.2, 4.0),  # cls loss gain
                'cls_pw': (1, 0.5, 2.0),  # cls BCELoss positive_weight
                'obj': (1, 0.2, 4.0),  # obj loss gain (scale with pixels)
                'obj_pw': (1, 0.5, 2.0),  # obj BCELoss positive_weight
                'iou_t': (0, 0.1, 0.7),  # IoU training threshold
                'anchor_t': (1, 2.0, 8.0),  # anchor-multiple threshold
                'anchors': (2, 2.0, 10.0),  # anchors per output grid (0 to ignore)
                'fl_gamma': (0, 0.0, 2.0),  # focal loss gamma (efficientDet default gamma=1.5)
                'hsv_h': (1, 0.0, 0.1),  # image HSV-Hue augmentation (fraction)
                'hsv_s': (1, 0.0, 0.9),  # image HSV-Saturation augmentation (fraction)
                'hsv_v': (1, 0.0, 0.9),  # image HSV-Value augmentation (fraction)
                'degrees': (1, 0.0, 45.0),  # image rotation (+/- deg)
                'translate': (1, 0.0, 0.9),  # image translation (+/- fraction)
                'scale': (1, 0.0, 0.9),  # image scale (+/- gain)
                'shear': (1, 0.0, 10.0),  # image shear (+/- deg)
                'perspective': (0, 0.0, 0.001),  # image perspective (+/- fraction), range 0-0.001
                'flipud': (1, 0.0, 1.0),  # image flip up-down (probability)
                'fliplr': (0, 0.0, 1.0),  # image flip left-right (probability)
                'mosaic': (1, 0.0, 1.0),  # image mixup (probability)
                'mixup': (1, 0.0, 1.0),   # image mixup (probability)
                'copy_paste': (1, 0.0, 1.0),  # segment copy-paste (probability)
                'paste_in': (1, 0.0, 1.0)}    # segment copy-paste (probability)

        with open(opt.hyp, errors='ignore') as f:
            hyp = yaml.safe_load(f)  # load hyps dict
            if 'anchors' not in hyp:  # anchors commented in hyp.yaml
                hyp['anchors'] = 3

        assert opt.local_rank == -1, 'DDP mode not implemented for --evolve'
        opt.notest, opt.nosave = True, True  # only test/save final epoch
        # ei = [isinstance(x, (int, float)) for x in hyp.values()]  # evolvable indices
        yaml_file = Path(opt.save_dir) / 'hyp_evolved.yaml'  # save best result here
        if opt.bucket:
            os.system('gsutil cp gs://%s/evolve.txt .' % opt.bucket)  # download evolve.txt if exists

        for _ in range(300):  # generations to evolve
            if Path('evolve.txt').exists():  # if evolve.txt exists: select best hyps and mutate
                # Select parent(s)
                parent = 'single'  # parent selection method: 'single' or 'weighted'
                x = np.loadtxt('evolve.txt', ndmin=2)
                n = min(5, len(x))  # number of previous results to consider
                x = x[np.argsort(-fitness(x))][:n]  # top n mutations
                w = fitness(x) - fitness(x).min()  # weights
                if parent == 'single' or len(x) == 1:
                    # x = x[random.randint(0, n - 1)]  # random selection
                    x = x[random.choices(range(n), weights=w)[0]]  # weighted selection
                elif parent == 'weighted':
                    x = (x * w.reshape(n, 1)).sum(0) / w.sum()  # weighted combination

                # Mutate
                mp, s = 0.8, 0.2  # mutation probability, sigma
                npr = np.random
                npr.seed(int(time.time()))
                g = np.array([x[0] for x in meta.values()])  # gains 0-1
                ng = len(meta)
                v = np.ones(ng)
                while all(v == 1):  # mutate until a change occurs (prevent duplicates)
                    v = (g * (npr.random(ng) < mp) * npr.randn(ng) * npr.random() * s + 1).clip(0.3, 3.0)
                for i, k in enumerate(hyp.keys()):  # plt.hist(v.ravel(), 300)
                    hyp[k] = float(x[i + 7] * v[i])  # mutate

            # Constrain to limits
            for k, v in meta.items():
                hyp[k] = max(hyp[k], v[1])  # lower limit
                hyp[k] = min(hyp[k], v[2])  # upper limit
                hyp[k] = round(hyp[k], 5)  # significant digits

            # Train mutation
            results, train_final = train(hyp.copy(), opt, device, target=target)

            # Write mutation results
            print_mutation(hyp.copy(), results, yaml_file, opt.bucket)

        # Plot results
        plot_evolution(yaml_file)
        logger.info(f'Hyperparameter evolution complete. Best results saved as: {yaml_file}\n'
                    f'Command to train a new model with these hyperparameters: $ python train.py --hyp {yaml_file}')

    # NAS ----------------------------------------------------------------------
    if nas or target == 'Galaxy_S22':
        #=======================================================================
        train_final = search(proj_info, hyp, opt, data, train_final)
        #=======================================================================

    # Model Export -------------------------------------------------------------
    if not train_final:
        logger.warn("train end but no trained weights")
        return None
    # target_acc = proj_info['acc']
    # target_engine = proj_info['engine'].replace('-', '').replace('_', '').lower()
    # task = proj_info['task_type']
    channel = data.get('ch')
    # print(f'channle = {channel}')
    convert = ['torchscript', 'onnx']
    export_weight(train_final, target_acc, convert, task=task, ch=channel, imgsz=opt.img_size)

    # optional
    if task == 'detection':
        export_weight(train_final, target_acc, ['onnx_end2end'])

    # src_bestmodel_path = COMMON_ROOT / userid / project_id / 'autonn' / 'weights' / 'best.torchscript'
    src_bestmodel_path = Path(train_final).with_suffix('.torchscript')
    dst_bestmodel_path = COMMON_ROOT / userid / project_id / 'bestmodel.torchscript'
    shutil.copyfile(str(src_bestmodel_path), str(dst_bestmodel_path))

    src_nninfo_path = CFG_PATH / 'neural_net_info.yaml'
    dst_nninfo_path = COMMON_ROOT / userid / project_id / 'neural_net_info.yaml'
    export_config(src_nninfo_path, dst_nninfo_path, data, basemodel, target_acc, target_engine, task=task)

    # logger.info('train finished')
    # mb = os.path.getsize(train_final) / 1E6  # filesize
    # logger.info(f'best model = {train_final}, {mb:.1f} MB')
    # logger.info(f'mAP = {results[-1]}')

    # Inference ----------------------------------------------------------------
    # version 1. call a function from main codes
    # from .detect import detect
    # weights = dst_bestmodel_path
    # cfg = COMMON_ROOT / userid / project_id / 'basemodel.yaml'
    # detect(weights, cfg, save_img=False)
    # INF_DIR = CORE_DIR / 'tango' / 'inference'

    # version 2. run an inference app directly
    # from tango.inference.detection_app import run
    # weights = './yolov9-m.torchscript'
    # source = str(INF_DIR / 'horses.jpg')
    # run(weights, source, view_img=False, save_img=True, save_txt=True, save_dir=str(INF_DIR))

    return train_final  # bestmodel.pt

