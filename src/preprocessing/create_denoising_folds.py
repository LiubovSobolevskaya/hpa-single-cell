import pickle
import numpy as np
import pandas as pd
import os
from sklearn.model_selection import KFold
from random import choice
from tqdm.auto import tqdm
import warnings
warnings.simplefilter("ignore")

from ..data.utils import get_public_df_ohe, get_train_df_ohe

N_FOLDS = 3

train_df = get_train_df_ohe(clean_from_duplicates=True,
                            clean_mitotic=True,
                            clean_aggresome=True)
img_paths_train = list(train_df['img_base_path'].values)
basepath_2_ohe_vector = {img:vec for img, vec in zip(train_df['img_base_path'], train_df.iloc[:, 2:].values)}


public_hpa_df_17 = get_public_df_ohe(clean_from_duplicates=True,
                                     clean_mitotic=True,
                                     clean_aggresome=True)
public_basepath_2_ohe_vector = {img_path:vec for img_path, vec in zip(public_hpa_df_17['img_base_path'],
                                                            public_hpa_df_17.iloc[:, 2:].values)}


basepath_2_ohe_vector.update(public_basepath_2_ohe_vector)


# mappings ids
img_base_path_2_id = dict()
img_id_2_base_path = dict()
for img_base_path in basepath_2_ohe_vector.keys():
    img_id = os.path.basename(img_base_path)
    img_base_path_2_id[img_base_path] = img_id
    img_id_2_base_path[img_id] = img_base_path


def get_id_2_masks(precomputed_masks_path):
    id_2_mask_indices = dict()
    for mask_path in tqdm(os.listdir(precomputed_masks_path),
                                     desc=f'Generating id_2_masks mapping for {precomputed_masks_path}'):
        img_id = mask_path.replace('.pkl', '')
        bboxes_path = os.path.join(precomputed_masks_path, mask_path)
        bboxes_df = pd.read_pickle(bboxes_path)
        id_2_mask_indices[img_id] = [cell_i for cell_i in bboxes_df.index]
    return id_2_mask_indices


id_2_mask_indices = get_id_2_masks('input/cell_bboxes_train')
id_2_mask_indices_public = get_id_2_masks('input/cell_bboxes_public')
id_2_mask_indices.update(id_2_mask_indices_public)


# Splitting masks into folds
fold_2_imgId_2_maskIndices = [dict() for _ in range(N_FOLDS)]
for img_id, mask_indices in tqdm(id_2_mask_indices.items(), desc='Splitting into folds..'):
    if len(mask_indices) == 0:
        continue
    if len(mask_indices) == 1:
        assert N_FOLDS == 3, "fold_i choice was hard-coded for N_FOLDS==3"
        fold_i = choice([2]*10 + [1]*7 + [0]*3)
        fold_2_imgId_2_maskIndices[fold_i][img_id] = mask_indices
        continue
    kf = KFold(n_splits=min(N_FOLDS, len(mask_indices)), shuffle=True, random_state=41)
    fold_i = 0 if len(mask_indices) >= N_FOLDS else np.random.randint(N_FOLDS)
    for _, fold_indices in kf.split(range(len(mask_indices))):
        mask_idx_fold = [mask_indices[i] for i in fold_indices]
        if img_id in fold_2_imgId_2_maskIndices[fold_i]:
            fold_2_imgId_2_maskIndices[fold_i][img_id].extend(mask_idx_fold)
        else:
            fold_2_imgId_2_maskIndices[fold_i][img_id] = mask_idx_fold
        fold_i = min(fold_i + 1, N_FOLDS - 1)


# fold sizes
for fold_i in range(N_FOLDS):
    size = 0
    for _, masks in fold_2_imgId_2_maskIndices[fold_i].items():
        size += len(masks)
    print(f'Fold {fold_i}: {size}')

with open('input/denoisining_folds.pkl', 'wb') as f:
    pickle.dump(fold_2_imgId_2_maskIndices, f)