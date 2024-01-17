import multiprocessing as mp

import numpy as np

from concurrent import futures
from scipy.ndimage import distance_transform_edt, binary_dilation
from sklearn.metrics import pairwise_distances

from skimage.measure import regionprops
from skimage.draw import line_nd
from tqdm import tqdm


# TODO update this
def compute_centroid_distances(segmentation, resolution, n_neighbors):
    # TODO enable eccentricity centers instead
    props = regionprops(segmentation)
    centroids = np.array([prop.centroid for prop in props])
    if resolution is not None:
        pass  # TODO scale the centroids

    pair_distances = pairwise_distances(centroids)
    return pair_distances


def compute_boundary_distances(segmentation, resolution, n_threads):

    seg_ids = np.unique(segmentation)[1:]
    n = len(seg_ids)

    pairwise_distances = np.zeros((n, n))
    end_points1 = np.zeros((n, n, 3), dtype="int")
    end_points2 = np.zeros((n, n, 3), dtype="int")

    def compute_distances_for_object(i):

        seg_id = seg_ids[i]
        distances, indices = distance_transform_edt(segmentation != seg_id, return_indices=True, sampling=resolution)

        for j in range(len(seg_ids)):
            if i >= j:
                continue

            ngb_id = seg_ids[j]

            mask = segmentation == ngb_id
            ngb_dist = distances.copy()
            ngb_dist[~mask] = np.inf
            min_point_ngb = np.unravel_index(np.argmin(ngb_dist), shape=mask.shape)

            min_dist = distances[min_point_ngb]

            min_point = tuple(ind[min_point_ngb] for ind in indices)
            pairwise_distances[i, j] = min_dist

            end_points1[i, j] = min_point
            end_points2[i, j] = min_point_ngb

    n_threads = mp.cpu_count() if n_threads is None else n_threads
    with futures.ThreadPoolExecutor(n_threads) as tp:
        list(tqdm(
            tp.map(compute_distances_for_object, range(n)), total=n, desc="Compute boundary distances"
        ))

    return pairwise_distances, end_points1, end_points2, seg_ids


def measure_pairwise_object_distances(
    segmentation,
    distance_type,
    resolution=None,
    n_threads=None,
    save_path=None,
):
    supported_distances = ("boundary", "centroid")
    assert distance_type in supported_distances
    if distance_type == "boundary":
        distances, endpoints1, endpoints2, seg_ids = compute_boundary_distances(segmentation, resolution, n_threads)
    elif distance_type == "centroid":
        raise NotImplementedError
        # TODO has to be adapted
        # distances, neighbors = compute_centroid_distances(segmentation, resolution)

    if save_path is not None:
        np.savez(
            save_path,
            distances=distances,
            endpoints1=endpoints1,
            endpoints2=endpoints2,
            seg_ids=seg_ids,
        )

    return distances, endpoints1, endpoints2, seg_ids


def extract_nearest_neighbors(pairwise_distances, seg_ids, n_neighbors, ignore_lower_diag=True):
    distance_matrix = pairwise_distances.copy()

    distance_matrix[np.diag_indices(len(distance_matrix))] = np.inf
    if ignore_lower_diag:
        distance_matrix[np.tril_indices_from(distance_matrix)] = np.inf

    neighbor_distances = np.sort(distance_matrix, axis=1)[:, :n_neighbors]
    neighbor_indices = np.argsort(distance_matrix, axis=1)[:, :n_neighbors]

    pairs = []
    for i, (dists, inds) in enumerate(zip(neighbor_distances, neighbor_indices)):
        seg_id = seg_ids[i]
        ngb_ids = [seg_ids[j] for j, dist in zip(inds, dists) if np.isfinite(dist)]
        pairs.extend([[seg_id, ngb_id] for ngb_id in ngb_ids if ngb_id > seg_id])

    return pairs


def create_distance_lines(measurement_path, n_neighbors=None, pairs=None, bb=None, scale=None):

    auto_dists = np.load(measurement_path)
    distances, seg_ids = auto_dists["distances"], list(auto_dists["seg_ids"])
    start_points, end_points = auto_dists["endpoints1"], auto_dists["endpoints2"]

    if pairs is None and n_neighbors is not None:
        pairs = extract_nearest_neighbors(distances, seg_ids, n_neighbors)
    elif pairs is None:
        pairs = [
            [id1, id2] for id1 in seg_ids for id2 in seg_ids if id1 < id2
        ]

    assert pairs is not None
    pair_indices = (
        np.array([seg_ids.index(pair[0]) for pair in pairs]),
        np.array([seg_ids.index(pair[1]) for pair in pairs])
    )

    pairs = np.array(pairs)
    distances = distances[pair_indices]
    start_points = start_points[pair_indices]
    end_points = end_points[pair_indices]

    if bb is not None:
        in_bb = np.where(
            (start_points[:, 0] > bb[0].start) & (start_points[:, 0] < bb[0].stop) &
            (start_points[:, 1] > bb[1].start) & (start_points[:, 1] < bb[1].stop) &
            (start_points[:, 2] > bb[2].start) & (start_points[:, 2] < bb[2].stop) &
            (end_points[:, 0] > bb[0].start) & (end_points[:, 0] < bb[0].stop) &
            (end_points[:, 1] > bb[1].start) & (end_points[:, 1] < bb[1].stop) &
            (end_points[:, 2] > bb[2].start) & (end_points[:, 2] < bb[2].stop)
        )

        pairs = pairs[in_bb]
        distances, start_points, end_points = distances[in_bb], start_points[in_bb], end_points[in_bb]

        offset = np.array([b.start for b in bb])[None]
        start_points -= offset
        end_points -= offset

    lines = np.array([[start, end] for start, end in zip(start_points, end_points)])

    if scale is not None:
        scale_factor = np.array(3 * [scale])[None, None]
        lines //= scale_factor

    properties = {
        "id_a": pairs[:, 0],
        "id_b": pairs[:, 1],
        "distance": distances,
    }
    return lines, properties


def keep_direct_distances(segmentation, measurement_path, line_dilation=0, scale=None):
    """Filter out all distances that are not direct.
    I.e. distances that cross another segmented object.
    """
    distance_lines, properties = create_distance_lines(measurement_path, scale=scale)

    ids_a, ids_b = properties["id_a"], properties["id_b"]
    filtered_ids_a, filtered_ids_b = [], []

    for i, line in tqdm(enumerate(distance_lines), total=len(distance_lines)):
        id_a, id_b = ids_a[i], ids_b[i]

        start, stop = line
        line = line_nd(start, stop, endpoint=True)

        if line_dilation > 0:
            # TODO make this more efficient, ideally by dilating the mask coordinates
            # instead of dilating the actual mask.
            # We turn the line into a binary mask and dilate it to have some tolerance.
            line_vol = np.zeros_like(segmentation)
            line_vol[line] = 1
            line_vol = binary_dilation(line_vol, iterations=line_dilation)
        else:
            line_vol = line

        # Check if we cross any other segments:
        # Extract the unique ids in the segmentation that overlap with the segmentation.
        # We count this as a direct distance if no other object overlaps with the line.
        line_seg_ids = np.unique(segmentation[line_vol])
        line_seg_ids = np.setdiff1d(line_seg_ids, [0, id_a, id_b])

        if len(line_seg_ids) == 0:  # No other objet is overlapping, we keep the line.
            filtered_ids_a.append(id_a)
            filtered_ids_b.append(id_b)

    print("Keeping", len(filtered_ids_a), "/", len(ids_a), "distance pairs")
    filtered_pairs = [[ida, idb] for ida, idb in zip(filtered_ids_a, filtered_ids_b)]
    return filtered_pairs