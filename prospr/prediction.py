from Bio import SeqIO
import os
import torch
import numpy as np
import random
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

from prospr.sequence import Sequence
from prospr.io import save
from prospr.dataloader import get_tensors
from prospr.nn import ProsprNetwork, load_model, CUDA, CROP_SIZE, DIST_BINS, ANGLE_BINS, SS_BINS, ASA_BINS, INPUT_DIM

IDEAL_BATCH_SIZE = 2 

def norm(thing):
    mean = np.mean(thing)
    stdev = np.std(thing)
    return (thing - mean) / stdev

def get_start_idxs():
    """determine possible start indices for maximal grid coverage depending on sequence length"""
    padding = CROP_SIZE // 2

    mods = [] 
    mods.append([padding])

    indices = [i for i in range(0,padding+1)]
    for i in range(1,padding+1):
        mods.append(indices[0:i])
    for i in range(padding+1, CROP_SIZE):
        mods.append(indices[i-padding:])
    
    return mods #list of possible start indices indexed by L % CROP_SIZE

def get_masks(shape=(64,64,64), real=True):
    """get mask for crop assembly based on how close i,j pair is to center"""
    if real: #want the central weighting
        mask = np.zeros(shape)
        step = shape[1] // 8
        start_value = 0.25
        for n in range(4):
            v = start_value * (n+1)
            mask[:,(step*n):CROP_SIZE-(step*n),(step*n):CROP_SIZE-(step*n)] = v
    else: #uniform mask
        mask = np.ones(shape)
    return mask, mask[:,shape[1]//2,:], mask[:,:,shape[2]//2] 


def predict_domain(sequence, model, num_offsets=10, real_mask=True):
    '''make prediction for entire protein domain via crop assembly and averaging'''
    seq = sequence.seq
    seq_len = len(seq)

    # randomly select i,j pairs for grid offsets
    # first pick from ones that give optimal (full) sequence coverage, then randomly select rest
    normal_offsets = get_start_idxs()[seq_len % CROP_SIZE]
    start_coords = []
    crop_list = []
    while len(start_coords) < num_offsets:
        if len(start_coords) >= (len(normal_offsets) **2): 
            possible_starts = [i for i in range(31)]
            start_i = random.choice(possible_starts)
            start_j = random.choice(possible_starts)
        else:
            start_i = random.choice(normal_offsets)
            start_j = random.choice(normal_offsets)
        if (start_i,start_j) not in start_coords: 
            start_coords.append((start_i,start_j))
            i = start_i
            j = start_j
            while i < seq_len:
                while j < seq_len:
                    crop_list.append((i, j))
                    j += CROP_SIZE
                j = start_j
                i += CROP_SIZE

    ss_sum = np.zeros([SS_BINS,seq_len])
    phi_sum = np.zeros([ANGLE_BINS,seq_len])
    psi_sum = np.zeros([ANGLE_BINS,seq_len])
    asa_sum = np.zeros([ASA_BINS,seq_len])
    dist_sum = np.zeros([DIST_BINS,seq_len,seq_len])
    
    dim2_ct = np.zeros([ANGLE_BINS,seq_len])
    dim3_ct = np.zeros([DIST_BINS,seq_len,seq_len])

    model = model.eval()

    BATCH_SIZE = IDEAL_BATCH_SIZE

    while len(crop_list) > 0:
        if len(crop_list) < IDEAL_BATCH_SIZE:
            BATCH_SIZE = len(crop_list)
        input_vector = torch.zeros([BATCH_SIZE,INPUT_DIM,CROP_SIZE,CROP_SIZE], dtype=torch.float, device=CUDA)
        batch_crops = []
        for batch in range(BATCH_SIZE):
            crop = crop_list.pop(0)
            input_vector[batch,:] = get_tensors(sequence, i=crop[0], j=crop[1]) 
            batch_crops.append(crop)

        with torch.no_grad():
            pred_dist, pred_aux_i, pred_aux_j = model(input_vector)

        batch_ss_i = pred_aux_i['ss'].cpu().detach().numpy()
        batch_ss_j = pred_aux_j['ss'].cpu().detach().numpy()
        batch_phi_i = pred_aux_i['phi'].cpu().detach().numpy()
        batch_phi_j = pred_aux_j['phi'].cpu().detach().numpy()
        batch_psi_i = pred_aux_i['psi'].cpu().detach().numpy()
        batch_psi_j = pred_aux_j['psi'].cpu().detach().numpy()
        batch_asa_i = pred_aux_i['asa'].cpu().detach().numpy()
        batch_asa_j = pred_aux_j['asa'].cpu().detach().numpy()
        batch_dist = pred_dist.cpu().detach().numpy()

        for batch in range(BATCH_SIZE):
            crop_id = batch_crops[batch]
            i = crop_id[0]
            j = crop_id[1]

            ss_i = batch_ss_i[batch] 
            ss_j = batch_ss_j[batch] 
            phi_i = batch_phi_i[batch]
            phi_j = batch_phi_j[batch]
            psi_i = batch_psi_i[batch]
            psi_j = batch_psi_j[batch]
            asa_i = batch_asa_i[batch]
            asa_j = batch_asa_j[batch]
            dist = batch_dist[batch] 

            mask, mask_i, mask_j = get_masks(shape=(ANGLE_BINS,CROP_SIZE,CROP_SIZE), real=real_mask)
            mask = mask[:DIST_BINS,:,:]

            #crop off padding
            if i > (seq_len-32): 
                ss_i = ss_i[:,:(CROP_SIZE-(i-(seq_len-32)))]
                phi_i = phi_i[:,:(CROP_SIZE-(i-(seq_len-32)))]
                psi_i = psi_i[:,:(CROP_SIZE-(i-(seq_len-32)))]
                asa_i = asa_i[:,:(CROP_SIZE-(i-(seq_len-32)))]
                dist = dist[:,:(CROP_SIZE-(i-(seq_len-32))),:]
                mask_i = mask_i[:,:(CROP_SIZE-(i-(seq_len-32)))]
                mask = mask[:,:(CROP_SIZE-(i-(seq_len-32))),:]
            if i < 32:
                ss_i = ss_i[:,(32-i):]
                phi_i = phi_i[:,(32-i):]
                psi_i = psi_i[:,(32-i):]
                asa_i = asa_i[:,(32-i):]
                dist = dist[:,(32-i):,:]
                mask_i = mask_i[:,(32-i):]
                mask = mask[:,(32-i):,:]
            if j > (seq_len-32): 
                ss_j = ss_j[:,:(CROP_SIZE-(j-(seq_len-32)))]
                phi_j = phi_j[:,:(CROP_SIZE-(j-(seq_len-32)))]
                psi_j = psi_j[:,:(CROP_SIZE-(j-(seq_len-32)))]
                asa_j = asa_j[:,:(CROP_SIZE-(j-(seq_len-32)))]
                dist = dist[:,:,:(CROP_SIZE-(j-(seq_len-32)))]
                mask_j = mask_j[:,:(CROP_SIZE-(j-(seq_len-32)))]
                mask = mask[:,:,:(CROP_SIZE-(j-(seq_len-32)))]
            if j < 32:
                ss_j = ss_j[:,(32-j):]
                phi_j = phi_j[:,(32-j):]
                psi_j = psi_j[:,(32-j):]
                asa_j = asa_j[:,(32-j):]
                dist = dist[:,:,(32-j):]
                mask_j = mask_j[:,(32-j):]
                mask = mask[:,:,(32-j):]

            # apply masks
            ss_i  *= mask_i[:SS_BINS,:]
            ss_j  *= mask_j[:SS_BINS,:]
            phi_i *= mask_i
            phi_j *= mask_j
            psi_i *= mask_i
            psi_j *= mask_j
            asa_i *= mask_i[:ASA_BINS,:]
            asa_j *= mask_j[:ASA_BINS,:]
            dist  *= mask 

            ss_i = norm(ss_i)
            ss_j = norm(ss_j)
            phi_i = norm(phi_i)
            phi_j = norm(phi_j)
            psi_i = norm(psi_i)
            psi_j = norm(psi_j)
            asa_i = norm(asa_i)
            asa_j = norm(asa_j)
            dist = norm(dist)
            
            dim_i = int(dist.shape[1])
            dim_j = int(dist.shape[2])
            start_i = np.max([0, i-32])
            start_j = np.max([0, j-32])
            
            ss_sum[:,start_i:(start_i+dim_i)] += ss_i
            ss_sum[:,start_j:(start_j+dim_j)] += ss_j
            phi_sum[:,start_i:(start_i+dim_i)] += phi_i
            phi_sum[:,start_j:(start_j+dim_j)] += phi_j
            psi_sum[:,start_i:(start_i+dim_i)] += psi_i
            psi_sum[:,start_j:(start_j+dim_j)] += psi_j
            asa_sum[:,start_i:(start_i+dim_i)] += asa_i
            asa_sum[:,start_j:(start_j+dim_j)] += asa_j
            dist_sum[:,start_i:(start_i+dim_i),start_j:(start_j+dim_j)] += dist
            
            #keep track of what's been predicted where and with which mask values
            dim2_ct[:,start_i:(start_i+int(dim_i))] += mask_i
            dim2_ct[:,start_j:(start_j+int(dim_j))] += mask_j
            dim3_ct[:,start_i:(start_i+int(dim_i)),start_j:(start_j+int(dim_j))] += mask

    #adjust predictions by masking sums
    dist_ct = dim3_ct
    ss_ct = dim2_ct[:SS_BINS,:]
    angle_ct = dim2_ct
    asa_ct = dim2_ct[:ASA_BINS,:]

    dist_avg = dist_sum / dist_ct
    ss_avg = ss_sum / ss_ct
    phi_avg = phi_sum / angle_ct
    psi_avg = psi_sum / angle_ct
    asa_avg = asa_sum / asa_ct

    return {'dist':dist_avg, 'ss':ss_avg, 'phi':phi_avg, 'psi':psi_avg, 'asa':asa_avg}

def predict(args):
    """Predict the features for the provided file (.a3m or .pdb)"""

    model_paths = []
    if args.network == 'all':
        model_paths = ['./nn/prospr0421_'+x+'.pt' for x in ['a','b','c']]
    elif args.network in ['a', 'b', 'c']:
        model_paths.append('./nn/prospr0421_'+args.network+'.pt')
    else:
        print('Invalid network selection!')
        return 

    # TODO: Accept fasta input, get a3m from hhblits
    # if not args.a3m:
        # # Write a fasta file with the sequence
        # with open(args.pdbfile) as handle:
        #     sequence = next(SeqIO.parse(handle, "pdb-atom"))
        # with open("example.fasta", "w") as output_handle:
        #     SeqIO.write(sequence, output_handle, "fasta")
        # # Get a3m from hhblits
        # cmd = 'hhblits -i example.fasta -oa3m example.a3m'
        # process = subprocess.Popen(cmd.split(), stdout=subprocess.PIPE)
        # output, error = process.communicate()
        # args.a3m = 'example.a3m'
    seq = Sequence(args.a3m)
    save_path = os.path.join(args.output_dir, seq.name+'_prediction.pkl')

    print('Buildling input vector...')
    seq.build()
    try:
        seq.seq
        seq.hhm
        seq.dca
    except:
        print('ERROR! unable to properly build input vector. Please check that input is in a3m format')
        
    total_pred = []
    ctr = 0

    print('Loading ProSPr model(s)...')
    for path in model_paths:
        prospr = ProsprNetwork()
        load_model(prospr, path)
        prospr.to(CUDA)
        print('Model location:',next(prospr.parameters()).device)
        print('Making predictions...')
        pred = predict_domain(sequence=seq, model=prospr)
        prospr = None

        if total_pred == []:
            total_pred = pred 
        else:
            for key,val in pred.items():
                total_pred[key] += val
        ctr +=1
    
    print('Saving results...')
    avg_pred = dict()
    avg_pred['domain'] = seq.name
    avg_pred['seq'] = seq.seq
    for key,val in total_pred.items():
        avg = val/ctr
        avg_pred[key] = torch.nn.functional.softmax(torch.tensor(avg), dim=0).numpy()
    nets = [p.split('nn/')[-1] for p in model_paths]
    avg_pred['network'] = ', '.join(nets)
    avg_pred['description'] = avg_pred['domain'] + ' predictions made with ' + avg_pred['network'] + ' using default settings: WEIGHTED crop assembly of 10 grids, reported as PROBABILITIES'
    avg_pred['dist_bin_map'] = [0, 4.001, 6.001, 8.001, 10.001, 12.001, 14.001, 16.001, 18.001, 20.001]
    
    if args.save:
        save(avg_pred, save_path)
        print('Done! Successfully saved at ', save_path)
        
    return avg_pred
