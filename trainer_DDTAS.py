# coding=utf-8
from __future__ import print_function, absolute_import
import time
from utils import AverageMeter, orth_reg
from utils.setBNeval import set_bn_eval
import torch
from torch.autograd import Variable
from torch.backends import cudnn
import models
from new_edite.dataparallel import DataParallel
import numpy

cudnn.benchmark = True

def Norm(w):
    w=torch.relu(w)
    s=torch.sum(w)
    w=w/s
    w=w/torch.max(w)#w is belong to (0,1)
    return w

def to_var(x, requires_grad=True):
    if torch.cuda.is_available():
        x = x.cuda()
    return Variable(x, requires_grad=requires_grad)

# Here, we annotate the code according to the number of steps in the algorithmic flow in our paper:

def train_DDTAS(epoch, model, criterion_i, criterion, criterion_m, optimizer, train_loader, valid_loader, margin, Glob, args , it_100, it_100_g):

    train_margin = margin
    losses = AverageMeter()
    batch_time = AverageMeter()
    accuracy = AverageMeter()
    pos_sims = AverageMeter()
    neg_sims = AverageMeter()

    end = time.time()

    freq = min(args.print_freq, len(train_loader))

    for i, data_ in enumerate(train_loader, 0):


        # ========================================================================
        # =========================== Outer Loop Start ===========================


        # Alg Flow.(1): initial Meta-Net 
        meta_net = models.create(args.net, args.data,pretrained=True, dim=args.dim)  
        meta_net = DataParallel(meta_net).cuda()
        meta_net.load_state_dict(model.state_dict())
        meta_net.apply(set_bn_eval)

        # Data augmentation images and lable from trainset
        inputs_t, labels_t = data_
        inputs_t, labels_t = to_var(inputs_t), to_var(labels_t, requires_grad=False)

        optimizer.zero_grad()

        # TrainSet Features from the Meta-Net have two way in outer loop.
        # On the one hand, these features are stored in the global dictionary. Alg Flow.(2)(5)
        # On the other hand, they are used for weight initialization. Alg Flow.(6)
        embed_feat_1 = meta_net(inputs_t)


      
        # -----------------------------------------------------------------------
        # ---------------------- Threshold Generation Process ----------------------

        # Trainset features' another direction --- weight initialization(Alg Flow.(6))
        margin_t = to_var(torch.ones(inputs_t.size(0)).mul(train_margin), requires_grad=False)
        _, anchor_list, ap_list, an_list, slct_num = criterion_i(embed_feat_1, labels_t, margin_t)
        weight = to_var(torch.zeros(slct_num).cuda())  # Alg Flow.(6)

        # Get similarity matrix(train) in DML loss , and calculate the loss and gradients (Alg Flow.(7))
        loss, inter_, dist_ap, dist_an = criterion(embed_feat_1, labels_t, margin_t, weight, ap_list, an_list)
        l_f_meta = loss
        # print('lf_meta', l_f_meta)
        param = meta_net.parameters()

        grads = torch.autograd.grad(l_f_meta, param, create_graph=True, allow_unused=True)

        # print('grads',grads)
        meta_net.update_params_t(args.lr, source_params=grads)  # update the temporary parameters(Alg Flow.(8))
        meta_dict_t = meta_net.state_dict()
        # print(meta_dict_t)
        meta_net.load_state_dict(meta_dict_t)

        # We use the KP sampler (P categories, K samples/category)
        # to generate a validation mini-batch from the validation set.
        image_v, labels_v = valid_loader.__next__()
        image_v, labels_v = to_var(image_v), to_var(labels_v, requires_grad=False)

        # Get features from validation set
        y_feat_2 = meta_net(image_v)

        # Features generated by the validation mini-batch and the global feature
        # from Semi-Global-Dictionary are fed into a pair-based DML loss.
        # l_g_metas = criterion_m(y_feat_2, labels_v, global_feature, global_label, args.margin)
        l_g_metas = criterion_m(y_feat_2, labels_v, y_feat_2, labels_v , args.margin)

        if l_g_metas==0:
            continue

        # The derivative of the weight is performed after the calculation of loss.
        # Finally, the derivative generates the weight list after scaling, normalization.
        # We get a list of weights that will go to the next module.
        grad_weight = torch.autograd.grad(l_g_metas, weight, only_inputs=True, allow_unused=True) #Alg Flow.(10) (11)

        # print('gw1', y_feat_2.size())
        # print(grad_weight)
        grad_weight = grad_weight[0]

        # print('listap',  sum(ap_list)+sum(an_list))
        # print('listan', an_list)
        w_grad_2 = -grad_weight

        # Alg Flow.(12)(13)
        w3 = Norm(w_grad_2)
        # print('wtsp', w3)
        # =========================== Outer Loop End ===========================
        # ======================================================================

        # =========================== Inner Loop ===============================
        s_it = time.time()
        embed_feat_3 = model(inputs_t)

        loss, inter_, dist_ap, dist_an = criterion(embed_feat_3, labels_t, margin_t, w3, ap_list, an_list)
        if args.orth_reg != 0:
            loss = orth_reg(net=model, loss=loss, cof=args.orth_reg)

        # Alg Flow.(14)
        loss.backward()
        # Alg Flow.(15)
        optimizer.step()

        batch_time.update(time.time() - end)
        end = time.time()

        losses.update(loss.item())
        e_it = time.time()
        # =========================== Inner Loop ===========================


        itt = e_it - s_it
        it_100.append(itt)
        # print('itteration_time', itt)
        # if len(it_100) ==3:
        # print('smtm',it_100)
        # print('lenit', len(it_100))
        if len(it_100) == 100:
            print('100it_time', sum(it_100))
            it_100_g.append(sum(it_100))
            print('m100it', len(it_100_g), sum(it_100_g) / len(it_100_g))
            it_100 = []
        if (i + 1) % freq == 0 or (i+1) == len(train_loader):
            print('Epoch: [{0:03d}][{1}/{2}]\t'
                  'Time {batch_time.avg:.3f}\t'
                  'Loss {loss.avg:.4f} \t'
                  'Accuracy {accuracy.avg:.4f} \t'
                  'Pos {pos.avg:.4f}\t'
                  'Neg {neg.avg:.4f} \t'.format
                  (epoch + 1, i + 1, len(train_loader), batch_time=batch_time,
                   loss=losses, accuracy=accuracy, pos=pos_sims, neg=neg_sims))


        if epoch == 0 and i == 0:
            print('-- HA-HA-HA-HA-AH-AH-AH-AH --')
    return losses, Glob, it_100, it_100_g
