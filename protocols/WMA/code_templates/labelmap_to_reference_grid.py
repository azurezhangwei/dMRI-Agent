#!/usr/bin/env python3
"""Resample a label/mask to the actual DWI voxel grid without reading 4D signal data."""
import argparse,json
from pathlib import Path
import numpy as np
import nibabel as nib
import nrrd
from scipy.ndimage import affine_transform


def nrrd_geometry(header):
    directions = np.asarray(header['space directions'], dtype=float)
    axes = np.flatnonzero(np.all(np.isfinite(directions), axis=1))
    if len(axes) != 3:
        raise ValueError('Reference must describe exactly three spatial axes')
    space = str(header['space']).lower()
    frames = {'right-anterior-superior': [1, 1, 1],
              'left-anterior-superior': [-1, 1, 1],
              'left-posterior-superior': [-1, -1, 1],
              'RAS': [1, 1, 1], 'LAS': [-1, 1, 1], 'LPS': [-1, -1, 1]}
    frames = {k.lower(): v for k, v in frames.items()}
    if space not in frames:
        raise ValueError('Unsupported NRRD world frame: ' + space)
    matrix = np.eye(4)
    matrix[:3, :3] = directions[axes].T
    matrix[:3, 3] = header['space origin']
    convert = np.eye(4)
    convert[:3, :3] = np.diag(frames[space])
    shape = tuple(int(header['sizes'][i]) for i in axes)
    return shape, convert @ matrix


def reference_geometry(path):
    if str(path).lower().endswith(('.nhdr', '.nrrd')):
        return nrrd_geometry(nrrd.read_header(str(path)))
    image = nib.load(str(path))
    return image.shape[:3], image.affine


def convert(input_path, reference_path, output_path, labels=None, binary=False):
    if str(input_path).lower().endswith(('.nhdr', '.nrrd')):
        data, header = nrrd.read(str(input_path), index_order='F')
        _, source_affine = nrrd_geometry(header)
    else:
        image = nib.load(str(input_path))
        data, source_affine = np.asarray(image.dataobj), image.affine
    if data.ndim != 3:
        raise ValueError('Input must be a three-dimensional labelmap or mask')
    if labels is not None:
        data = np.isin(data, labels).astype(np.uint8)
    elif binary:
        data = (data > 0).astype(np.uint8)
    else:
        if not np.isfinite(data).all() or data.min() < 0 or data.max() > 255 or not np.equal(data, np.round(data)).all():
            raise ValueError('Use explicit --labels or --binary for input that is not a uint8 labelmap')
        data = data.astype(np.uint8)
    shape, target_affine = reference_geometry(reference_path)
    mapping = np.linalg.inv(source_affine) @ target_affine
    output = affine_transform(data, mapping[:3, :3], offset=mapping[:3, 3],
                              output_shape=shape, order=0, mode='constant', cval=0,
                              prefilter=False).astype(np.uint8)
    header = {'space': 'right-anterior-superior',
              'space directions': target_affine[:3, :3].T,
              'space origin': target_affine[:3, 3],
              'kinds': ['domain', 'domain', 'domain'], 'encoding': 'gzip'}
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    nrrd.write(str(output_path), output, header, index_order='F')
    return {'output': str(output_path), 'shape': list(shape),
            'nonzero_voxels': int(np.count_nonzero(output)),
            'reference': str(reference_path), 'output_ijk_to_ras': target_affine.tolist(),
            'output_to_source_ijk': mapping.tolist()}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', required=True, help='Existing NIfTI/NRRD labelmap or mask')
    p.add_argument('--reference', required=True, help='Actual DWI NHDR/NRRD or NIfTI; header only')
    p.add_argument('--output', required=True, help='New uint8 NRRD path')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--labels', nargs='+', type=int, help='Selected anatomical IDs to binarize')
    g.add_argument('--binary', action='store_true', help='Treat every positive input value as mask')
    a = p.parse_args()
    if Path(a.input).resolve() == Path(a.output).resolve():
        p.error('Use a separate output path to preserve the source labelmap')
    print(json.dumps(convert(a.input, a.reference, a.output, a.labels, a.binary)))

if __name__ == '__main__':
    main()
