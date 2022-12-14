#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
File: source/model/seq2seq.py
"""

import numpy as np
import matplotlib.pyplot as plt
from sklearn.manifold import TSNE

import torch
import torch.nn as nn
from torch.distributions import Normal

from source.model.base_model import BaseModel
from source.module.embedder import Embedder
from source.module.rnn_encoder import RNNEncoder
from source.module.rnn_decoder import RNNDecoder
from source.utils.criterions import NLLLoss, MaskBCELoss
from source.utils.metrics import accuracy
from source.utils.rewards import reward_fn
from source.utils.misc import Pack
from source.utils.misc import sequence_mask


class Seq2Seq(BaseModel):
    """
    Seq2Seq
    """
    def __init__(self,
                 src_field,
                 tgt_field,
                 kb_field,
                 embed_size,
                 hidden_size,
                 qr,
                 cl,
                 kl,
                 padding_idx=None,
                 num_layers=1,
                 bidirectional=False,
                 attn_mode="mlp",
                 with_bridge=False,
                 tie_embedding=False,
                 max_hop=1,
                 dropout=0.0,
                 use_gpu=False):
        super(Seq2Seq, self).__init__()

        self.src_field = src_field
        self.tgt_field = tgt_field
        self.kb_field = kb_field
        self.src_vocab_size = src_field.vocab_size
        self.tgt_vocab_size = tgt_field.vocab_size
        self.kb_vocab_size = kb_field.vocab_size
        self.embed_size = embed_size
        self.hidden_size = hidden_size
        self.padding_idx = padding_idx
        self.num_layers = num_layers
        self.bidirectional = bidirectional
        self.attn_mode = attn_mode
        self.with_bridge = with_bridge
        self.tie_embedding = tie_embedding
        self.max_hop = max_hop
        self.dropout = dropout
        self.use_gpu = use_gpu
        self.qr=qr
        self.cl=cl
        self.kl=kl

        self.BOS = self.tgt_field.stoi[self.tgt_field.bos_token]
        self.reward_fn_ = reward_fn

        self.enc_embedder = Embedder(num_embeddings=self.src_vocab_size,
                                     embedding_dim=self.embed_size,
                                     padding_idx=self.padding_idx)

        self.encoder = RNNEncoder(input_size=self.embed_size,
                                  hidden_size=self.hidden_size,
                                  embedder=self.enc_embedder,
                                  num_layers=self.num_layers,
                                  bidirectional=self.bidirectional,
                                  dropout=self.dropout)

        self.goaler = nn.Sequential(
            nn.Linear(self.hidden_size, self.hidden_size*2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(self.hidden_size*2, self.hidden_size*2),
        )

        self.Q_TD = nn.Sequential(
            nn.Linear(self.hidden_size, self.hidden_size*2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(self.hidden_size*2, 1),
        )

        self.projector = nn.Sequential(
            nn.Linear(self.hidden_size, self.hidden_size//2),
            nn.ReLU(),
        )

        # self.classifier = nn.Sequential(
        #     nn.Linear(hidden_size, hidden_size*2),
        #     nn.ReLU(),
        #     nn.Linear(hidden_size*2, 3),
        # )

        # self.classifier1 = nn.Sequential(
        #     nn.Linear(hidden_size, hidden_size*4),
        #     nn.ReLU(),
        #     nn.Linear(hidden_size*4, hidden_size*2),
        #     nn.ReLU(),
        #     nn.Linear(hidden_size*2, hidden_size),
        # )

        # self.classifier2 = nn.Sequential(
        #     nn.ReLU(),
        #     nn.Linear(hidden_size, 3),
        # )

        self.dialog_id = None
        self.en_td = None
        self.gl_cl = None
        self.kb_cl = None
        self.current_gl = None

        if self.with_bridge:
            self.bridge = nn.Sequential(
                nn.Linear(self.hidden_size, self.hidden_size),
                nn.Tanh(),
            )

        if self.tie_embedding:
            assert self.src_vocab_size == self.tgt_vocab_size
            self.dec_embedder = self.enc_embedder
            self.kb_embedder = self.enc_embedder
        else:
            self.dec_embedder = Embedder(num_embeddings=self.tgt_vocab_size,
                                         embedding_dim=self.embed_size,
                                         padding_idx=self.padding_idx)
            self.kb_embedder = Embedder(num_embeddings=self.kb_vocab_size,
                                        embedding_dim=self.embed_size,
                                        padding_idx=self.padding_idx)

        # TODO: change KB to MLP transform
        self.trans_layer = nn.Linear(3 * self.embed_size, self.hidden_size, bias=True)

        # init memory
        self.dialog_state_memory = None
        self.dialog_history_memory = None
        self.memory_masks = None
        self.kbs = None
        self.kb_state_memory = None
        self.kb_slot_memory = None
        self.history_index = None
        self.kb_slot_index = None
        self.kb_mask = None
        self.selector_mask = None

        self.decoder = RNNDecoder(embedder=self.dec_embedder,
                                  max_hop=self.max_hop,
                                  input_size=self.embed_size,
                                  hidden_size=self.hidden_size,
                                  output_size=self.tgt_vocab_size,
                                  kb_output_size=self.kb_vocab_size,
                                  num_layers=self.num_layers,
                                  attn_mode=self.attn_mode,
                                  memory_size=self.hidden_size,
                                  kb_memory_size=self.hidden_size,  # Note: hidden_size if MLP
                                  dropout=self.dropout,
                                  padding_idx=self.padding_idx,
                                  use_gpu=self.use_gpu)
        self.sigmoid = nn.Sigmoid()

        # loss definition
        if self.padding_idx is not None:
            weight = torch.ones(self.tgt_vocab_size)
            weight[self.padding_idx] = 0
        else:
            weight = None
        self.nll_loss = NLLLoss(weight=weight,
                                ignore_index=self.padding_idx,
                                reduction='mean')
        self.bce_loss = MaskBCELoss()

        if self.use_gpu:
            self.cuda()

    def reset_memory(self):
        """
        reset memory
        """
        self.dialog_state_memory = None
        self.dialog_history_memory = None
        self.memory_masks = None
        self.kbs = None
        self.kb_state_memory = None
        self.kb_slot_memory = None
        self.history_index = None
        self.kb_slot_index = None
        self.kb_mask = None
        self.selector_mask = None

    def load_kb_memory(self, kb_inputs):
        """
        load kb memory
        """
        kbs, kb_lengths = kb_inputs
        if self.use_gpu:
            kbs = kbs.cuda()
            kb_lengths = kb_lengths.cuda()

        batch_size, kb_num, kb_term = kbs.size()
        kbs = kbs[:, :, 1:-1]       # filter <bos> <eos>
        self.kbs = kbs

        # TODO: change kb_states
        #kb_states = kbs[:, :, :-1]  # <subject, relation>
        kb_states = kbs
        kb_slots = kbs[:, :, -1]    # <object>
        kb_states = kb_states.contiguous().view(batch_size, kb_num, -1)  # (batch_size, kb_num, 3)
        kb_slots = kb_slots.contiguous().view(batch_size, kb_num)  # (batch_size, kb_num)
        self.kb_slot_index = kb_slots

        kb_mask = kb_lengths.eq(0)
        self.kb_mask = kb_mask      # (batch_size, kb_num)
        selector_mask = kb_mask.eq(0)
        self.selector_mask = selector_mask  # (batch_size, kb_num)

        embed_state = self.kb_embedder(kb_states)
        embed_state = embed_state.contiguous().view(batch_size, kb_num, -1)
        self.kb_state_memory = self.trans_layer(embed_state)
        self.kb_slot_memory = self.kb_state_memory.clone()

    def dia_label(self, tasks):
        task = tasks.copy()
        for i in range(len(task)):
            if (task[i] == 'restaurant') or (task[i] == 'weather'):
                task[i] = 0
            elif (task[i] == 'hotel') or (task[i] == 'navigate'):
                task[i] = 1
            else:
                task[i] = 2
        task = torch.Tensor(task).to(torch.long).cuda()
        return task

    def TD_error_loss(self, state, statep, reward=None):
        Qvalue = self.Q_TD(state)
        with torch.no_grad():
            Qtarget = self.Q_TD(statep)

        loss = nn.functional.mse_loss(Qvalue, -1 + 0.9*Qtarget)

        return loss
    
    def contrastiveloss(self, cls_feat, anch_feat, cl_temp=0.5):
        # -a[torch.arange(len(b)),b] + torch.log(torch.sum(torch.exp(a), 1))
        cls_feat_n = nn.functional.normalize(cls_feat)
        anch_feat_n = nn.functional.normalize(anch_feat)
        
        sim_mat = torch.matmul(cls_feat_n, anch_feat_n.t()) / cl_temp

        logits = sim_mat.t()

        y = torch.arange(logits.size(0)).cuda(non_blocking=True)

        loss = nn.functional.cross_entropy(logits, y)

        return loss

    def label_accuracy(self, y, label):
        self.account = self.account + 1
        y = y.argmax(dim=1).view(-1)
        acc = ( 1 * (y == label) ).sum() / len(label)
        self.labelacc += acc.item()

    def draw(self, vec, note=""):
        vec = vec.detach().cpu()
        tsne = TSNE(n_components=2)
        # for i in range(len(vec)):
        #     tsne.fit_transform(vec[i])
        #     y =tsne.embedding_
        #     plt.scatter(y[:,0], y[:,1])
        #     plt.savefig("./image/tsne_"+note+ str(i) +".png")
        tsne.fit_transform(vec)
        y =tsne.embedding_
        plt.scatter(y[:,0], y[:,1], label=note)
        plt.legend()
        plt.savefig("./image/tsne_"+note+ ".png")

    def vae_goal(self, enc_inputs):
        mu_sigma = self.goaler(enc_inputs)
        mu, sigma = mu_sigma[:,:self.hidden_size], mu_sigma[:,self.hidden_size:]
        dist = Normal(mu, nn.functional.softplus(sigma))
        latent = dist.rsample()
        if self.training:
            return latent, mu, sigma
        else:
            return mu, mu, sigma

    def encode(self, enc_inputs, hidden=None):
        """
        encode
        """
        outputs = Pack()
        enc_outputs, enc_hidden = self.encoder(enc_inputs, hidden)
        # self.draw(enc_outputs[:,-1,:], "before")
        
        # goal_rep, e_mu, e_sigma = self.vae_goal(enc_outputs[:, -1, :])
        goal_rep, e_mu, e_sigma = self.vae_goal(torch.mean(enc_outputs, 1))
        goalproj = self.projector(goal_rep)
        self.current_gl = goalproj.detach()
        loss_kl = torch.mean(torch.exp(e_sigma) - (1+e_sigma) + torch.square(e_mu)).cuda()

        # for i in range(len(self.gl_cl)):
        #     self.en_td[i] = torch.cat((self.en_td[i], enc_outputs[i:(i+1),-1,:]))
        #     self.gl_cl[i] = torch.cat((self.gl_cl[i], goal_rep[i:(i+1),:]))
        self.gl_cl = torch.cat((self.gl_cl, goalproj))

        enc_outputs = enc_outputs + goal_rep.unsqueeze(1)

        # label_output = self.classifier(goal_rep)
        # # label_output = self.classifier1(tsnedata)
        # label_loss = nn.functional.cross_entropy(label_output, self.dialog_id)
        # loss_kl += 2 * label_loss
        # tsnedata = None

        # self.label_accuracy(label_output, self.dialog_id)

        # self.draw(enc_outputs[:,-1,:]+torch.randn(enc_outputs.size(0),256).cuda()*50, "after")

        # with open('./tmp/goal.npy', 'ab') as f:
        #     np.save(f, enc_outputs.cpu().numpy())
        
        # print(dd)
        inputs, lengths = enc_inputs
        batch_size = enc_outputs.size(0)
        max_len = enc_outputs.size(1)
        attn_mask = sequence_mask(lengths, max_len).eq(0)

        if self.with_bridge:
            # enc_hidden = self.bridge(enc_hidden)
            enc_hidden = self.bridge(goal_rep.unsqueeze(0))

        # insert dialog memory
        if self.dialog_state_memory is None:
            assert self.dialog_history_memory is None
            assert self.history_index is None
            assert self.memory_masks is None
            self.dialog_state_memory = enc_outputs
            self.dialog_history_memory = enc_outputs
            self.history_index = inputs
            self.memory_masks = attn_mask
        else:
            batch_state_memory = self.dialog_state_memory[:batch_size, :, :]
            self.dialog_state_memory = torch.cat([batch_state_memory, enc_outputs], dim=1)
            batch_history_memory = self.dialog_history_memory[:batch_size, :, :]
            self.dialog_history_memory = torch.cat([batch_history_memory, enc_outputs], dim=1)
            batch_history_index = self.history_index[:batch_size, :]
            self.history_index = torch.cat([batch_history_index, inputs], dim=-1)
            batch_memory_masks = self.memory_masks[:batch_size, :]
            self.memory_masks = torch.cat([batch_memory_masks, attn_mask], dim=-1)

        batch_kb_inputs = self.kbs[:batch_size, :, :]
        batch_kb_state_memory = self.kb_state_memory[:batch_size, :, :]
        batch_kb_slot_memory = self.kb_slot_memory[:batch_size, :, :]
        batch_kb_slot_index = self.kb_slot_index[:batch_size, :]
        kb_mask = self.kb_mask[:batch_size, :]
        selector_mask = self.selector_mask[:batch_size, :]

        # create batched KB inputs
        kb_memory, selector, attn = self.decoder.initialize_kb(kb_inputs=batch_kb_inputs, enc_hidden=enc_hidden)

        # initialize decoder state
        dec_init_state = self.decoder.initialize_state(
            hidden=enc_hidden,
            state_memory=self.dialog_state_memory,
            history_memory=self.dialog_history_memory,
            kb_memory=kb_memory,
            kb_state_memory=batch_kb_state_memory,
            kb_slot_memory=batch_kb_slot_memory,
            history_index=self.history_index,
            kb_slot_index=batch_kb_slot_index,
            attn_mask=self.memory_masks,
            attn_kb_mask=kb_mask,
            selector=selector,
            selector_mask=selector_mask,
            attn=attn,
            tsnedata = None
        )

        return outputs, dec_init_state, loss_kl

    def decode(self, dec_inputs, state):
        """
        decode
        """
        prob, attn_prob, kb_prob, p_gen, p_con, state = self.decoder.decode(dec_inputs, state)

        # logits copy from dialog history
        batch_size, max_len, word_size = prob.size()
        copy_index = state.history_index.unsqueeze(1).expand_as(
            attn_prob).contiguous().view(batch_size, max_len, -1)
        copy_logits = attn_prob.new_zeros(size=(batch_size, max_len, word_size),
                                          dtype=torch.float)
        copy_logits = copy_logits.scatter_add(dim=2, index=copy_index, src=attn_prob)

        # logits copy from kb
        index = state.kb_slot_index[:batch_size, :].unsqueeze(1).expand_as(
            kb_prob).contiguous().view(batch_size, max_len, -1)
        kb_logits = kb_prob.new_zeros(size=(batch_size, max_len, word_size),
                                      dtype=torch.float)
        kb_logits = kb_logits.scatter_add(dim=2, index=index, src=kb_prob)

        # compute final distribution
        con_logits = p_gen * prob + (1 - p_gen) * copy_logits
        logits = p_con * kb_logits + (1 - p_con) * con_logits
        log_logits = torch.log(logits + 1e-12)

        return log_logits, state

    def forward(self, enc_inputs, dec_inputs, hidden=None):
        """
        forward

        """
        outputs, dec_init_state, loss_kl = self.encode(enc_inputs, hidden)
        prob, attn_prob, kb_prob, p_gen, p_con, dec_state = self.decoder(dec_inputs, dec_init_state)

        # logits copy from dialog history
        batch_size, max_len, word_size = prob.size()
        copy_index = dec_init_state.history_index.unsqueeze(1).expand_as(
            attn_prob).contiguous().view(batch_size, max_len, -1)
        copy_logits = attn_prob.new_zeros(size=(batch_size, max_len, word_size),
                                          dtype=torch.float)
        copy_logits = copy_logits.scatter_add(dim=2, index=copy_index, src=attn_prob)

        # logits copy from kb
        index = dec_init_state.kb_slot_index[:batch_size, :].unsqueeze(1).expand_as(
            kb_prob).contiguous().view(batch_size, max_len, -1)
        kb_logits = kb_prob.new_zeros(size=(batch_size, max_len, word_size),
                                      dtype=torch.float)
        kb_logits = kb_logits.scatter_add(dim=2, index=index, src=kb_prob)

        # compute final distribution
        con_logits = p_gen * prob + (1-p_gen) * copy_logits
        logits = p_con * kb_logits + (1 - p_con) * con_logits
        log_logits = torch.log(logits + 1e-12)

        # # copy prob
        # pvoc = (1 - p_con) * p_gen
        # pmem = (1 - p_con) * (1 - p_gen)
        # pkb = p_con
        # copy_prob = torch.cat((pvoc, pmem, pkb), 2).cpu().detach().numpy()
        # probfile = "./copyprob/copydata.npy"
        # try:
        #     b = np.load(probfile)
        #     if b.shape[1] > copy_prob.shape[1]:
        #         copy_prob = np.append(copy_prob, np.zeros((copy_prob.shape[0], b.shape[1]-copy_prob.shape[1], copy_prob.shape[2])), 1)
        #     elif b.shape[1] < copy_prob.shape[1]:
        #         b = np.append(b, np.zeros((b.shape[0], copy_prob.shape[1]-b.shape[1], b.shape[2])), 1)
        #     c = np.append(b, copy_prob, 0)
        #     np.save(probfile, c)
        # except:
        #     np.save(probfile, copy_prob)
        # # copy prob end

        gate_logits = p_con.squeeze(-1)
        selector_logits = dec_init_state.selector

        # with open('./tmp/goal.npy', 'ab') as f_num:
        #     torch.argmax(selector_logits, 1)
        #     kbnum = self.kbs[torch.arange(selector_logits.size(0)), torch.argmax(selector_logits, 1)]
        #     np.save(f_num, kbnum.cpu().numpy())
        #     with open('./tmp/kb_name.txt', 'a') as f_name:
        #         kbname = self.kb_field.denumericalize(kbnum)
        #         for i in range(selector_logits.size(0)):
        #             f_name.write(kbname[i])
        #             f_name.write("\n")


        selector_mask = dec_init_state.selector_mask

        outputs.add(logits=log_logits, gate_logits=gate_logits,
                    selector_logits=selector_logits, selector_mask=selector_mask,
                    dialog_state_memory=dec_state.state_memory,
                    kb_state_memory=dec_state.kb_state_memory)
        return outputs, loss_kl

    def sample(self, enc_inputs, dec_inputs, hidden=None, random_sample=False):
        """
        sampling for RL training
        """
        outputs, dec_init_state, loss_kl = self.encode(enc_inputs, hidden)

        batch_size, max_len = dec_inputs[0].size()
        pred_log_logits = torch.zeros((batch_size, max_len, self.tgt_vocab_size))  # zeros equal to padding idx
        pred_word = torch.ones((batch_size, max_len),
                               dtype=torch.long) * self.padding_idx  
        state = dec_init_state

        for i in range(max_len):
            if i == 0:
                dec_inputs = torch.ones((batch_size), dtype=torch.long) * self.BOS
                if self.use_gpu:
                    dec_inputs = dec_inputs.cuda()
                    pred_log_logits = pred_log_logits.cuda()
                    pred_word = pred_word.cuda()
            if i >= 1:
                if i == 1:
                    unfinish = dec_inputs.ne(self.padding_idx).long()
                else:
                    unfinish = unfinish * (dec_inputs.ne(self.padding_idx)).long()
                if unfinish.sum().item() == 0:
                    break

            log_logits, state = self.decode(dec_inputs, state)
            pred_log_logits[:, i:i + 1] = log_logits

            if random_sample:
                dec_inputs = torch.multinomial(torch.exp(log_logits.squeeze()), num_samples=1).view(-1)
            else:
                dec_inputs = torch.argmax(log_logits.squeeze(), dim=-1).view(-1)
            pred_word[:, i] = dec_inputs

        outputs.add(logits=pred_log_logits, pred_word=pred_word)
        return outputs

    def reward_fn(self, *args):
        return self.reward_fn_(self, *args)

    def collect_metrics(self, outputs, target, ptr_index, kb_index):
        """
        collect training metrics
        """
        num_samples = target.size(0)
        metrics = Pack(num_samples=num_samples)
        loss = 0

        with torch.no_grad():
            # contrastive learning Q value
            qgl = nn.functional.normalize(self.current_gl)

            # qkb = nn.functional.normalize(outputs.dialog_state_memory[:, -1, :])
            qkb = nn.functional.normalize(self.projector(torch.mean(outputs.dialog_state_memory, 1)))
            
            q_value = torch.matmul(qgl, qkb.T).diag().exp() * self.qr
            # q_value = nn.functional.normalize(q_value, dim=0)
            # # TD error learning Q value
            # q_value = self.Q_TD(outputs.dialog_state_memory[:, -1, :]).squeeze()


        # loss for generation
        logits = outputs.logits
        nll = self.nll_loss(logits, target, q_value)
        loss += nll

        '''
        # loss for gate
        pad_zeros = torch.zeros([num_samples, 1], dtype=torch.long)
        if self.use_gpu:
            pad_zeros = pad_zeros.cuda()
        ptr_index = torch.cat([ptr_index, pad_zeros], dim=-1).float()
        gate_logits = outputs.gate_logits
        loss_gate = self.bce_loss(gate_logits, ptr_index)
        loss += loss_gate
        '''

        # loss for selector
        selector_target = kb_index.float()
        selector_logits = outputs.selector_logits
        selector_mask = outputs.selector_mask

        if selector_target.size(-1) < selector_logits.size(-1):
            pad_zeros = torch.zeros(size=(num_samples, selector_logits.size(-1)-selector_target.size(-1)),
                                    dtype=torch.float).to(selector_target.device)
            selector_target = torch.cat([selector_target, pad_zeros], dim=-1)
        
        loss_ptr = self.bce_loss(selector_logits, selector_target, mask=selector_mask)
        loss += loss_ptr

        acc = accuracy(logits, target, padding_idx=self.padding_idx)
        metrics.add(loss=loss, ptr=loss_ptr, acc=acc)

        return metrics

    def collect_rl_metrics(self, sample_outputs, greedy_outputs, target, gold_entity, entity_dir):
        """
        collect rl training metrics
        """
        num_samples = target.size(0)
        rl_metrics = Pack(num_samples=num_samples)
        loss = 0

        # log prob for sampling and greedily generation
        logits = sample_outputs.logits
        sample = sample_outputs.pred_word[:, -1, :]
        greedy_logits = greedy_outputs.logits
        greedy_sample = greedy_outputs.pred_word

        # cal reward
        sample_reward, _, _ = self.reward_fn(sample, target, gold_entity, entity_dir)
        greedy_reward, bleu_score, f1_score = self.reward_fn(greedy_sample, target, gold_entity, entity_dir)
        reward = sample_reward - greedy_reward

        # cal RL loss

        with torch.no_grad():
            qgl = nn.functional.normalize(self.current_gl)
            qkb = nn.functional.normalize(outputs.dialog_state_memory[:, -1, :])
            q_value = torch.matmul(qgl, qkb.T)

        sample_log_prob = self.nll_loss(logits, sample, q_value, mask=sample.ne(self.padding_idx),
                                        reduction=False, matrix=False)  # [batch_size, max_len]
        nll = sample_log_prob * reward.to(sample_log_prob.device)
        nll = getattr(torch, self.nll_loss.reduction)(nll.sum(dim=-1))
        loss += nll

        # gen report
        rl_acc = accuracy(greedy_logits, target, padding_idx=self.padding_idx)
        if reward.dim() == 2:
            reward = reward.sum(dim=-1)
        rl_metrics.add(loss=loss, reward=reward.mean(), rl_acc=rl_acc,
                       bleu_score=bleu_score.mean(), f1_score=f1_score.mean())

        return rl_metrics

    def update_memory(self, dialog_state_memory, kb_state_memory):
        self.dialog_state_memory = dialog_state_memory
        self.kb_state_memory = kb_state_memory

    def iterate(self, turn_inputs, kb_inputs,
                optimizer=None, grad_clip=None, use_rl=False, 
                entity_dir=None, is_training=True):
        """
        iterate
        """
        self.reset_memory()

        turn_batch_size = len(turn_inputs[0].src[1])
        self.en_td = [torch.Tensor([]).cuda() for i in range(turn_batch_size)]
        self.gl_cl = torch.Tensor([]).cuda()
        self.kb_cl = torch.Tensor([]).cuda()

        self.load_kb_memory(kb_inputs)

        metrics_list = []
        total_loss = 0
        kl_loss = 0

        for i, inputs in enumerate(turn_inputs):
            if self.use_gpu:
                inputs = inputs.cuda()
            src, src_lengths = inputs.src
            tgt, tgt_lengths = inputs.tgt
            gold_entity = inputs.gold_entity
            ptr_index, ptr_lengths = inputs.ptr_index
            kb_index, kb_index_lengths = inputs.kb_index
            enc_inputs = src[:, 1:-1], src_lengths - 2  # filter <bos> <eos>
            dec_inputs = tgt[:, :-1], tgt_lengths - 1  # filter <eos>
            target = tgt[:, 1:]  # filter <bos>
            self.dialog_id = self.dia_label(inputs.task)

            if use_rl:
                assert entity_dir is not None
                sample_outputs = self.sample(enc_inputs, dec_inputs, random_sample=True)
                with torch.no_grad():
                    greedy_outputs = self.sample(enc_inputs, dec_inputs, random_sample=False)
                    outputs, loss_kl = self.forward(enc_inputs, dec_inputs)
                metrics = self.collect_rl_metrics(sample_outputs, greedy_outputs, target,
                                                  gold_entity, entity_dir)
            else:
                outputs, loss_kl = self.forward(enc_inputs, dec_inputs)
                metrics = self.collect_metrics(outputs, target, ptr_index, kb_index)

            # for j in range(len(outputs.dialog_state_memory)):
            #     self.kb_cl[j] = torch.cat((self.kb_cl[j], outputs.dialog_state_memory[j:(j+1), -1, :]))
            
            # self.kb_cl = torch.cat((self.kb_cl, outputs.dialog_state_memory[:, -1, :]))
            self.kb_cl = torch.cat((self.kb_cl, self.projector(torch.mean(outputs.dialog_state_memory, 1))))

            metrics_list.append(metrics)
            total_loss += metrics.loss
            kl_loss += loss_kl.item()

            self.update_memory(dialog_state_memory=outputs.dialog_state_memory,
                               kb_state_memory=outputs.kb_state_memory)

        # cqloss = 0
        # for i in range(turn_batch_size):
        #     # cqloss += self.TD_error_loss(self.en_td[i], self.kb_cl[i]).item()
        #     # total_loss += 0.5 * self.TD_error_loss(self.en_td[i], self.kb_cl[i])
        #     cqloss += self.contrastiveloss(self.gl_cl[i], self.kb_cl[i]).item()
        #     total_loss += 0.5 * self.contrastiveloss(self.gl_cl[i], self.kb_cl[i])
        cqloss = self.contrastiveloss(self.gl_cl, self.kb_cl)
        total_loss += self.cl * cqloss

        total_loss += self.kl * loss_kl
        # with open ("./notelossc.txt", "a") as file:
        #     file.write(str(cqloss))
        #     file.write(" ")
        #     file.write(str(total_loss.item()))
        #     file.write("\n")

        if torch.isnan(total_loss):
            raise ValueError("NAN loss encountered!")

        if is_training:
            assert optimizer is not None
            optimizer.zero_grad()
            total_loss.backward()
            if grad_clip is not None and grad_clip > 0:
                torch.nn.utils.clip_grad_norm_(parameters=self.parameters(), max_norm=grad_clip)
            optimizer.step()

        return metrics_list
