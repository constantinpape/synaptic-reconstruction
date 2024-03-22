import os
from pathlib import Path

from tqdm import tqdm
from elf.io import open_file

from synaptic_reconstruction.file_utils import get_data_path
from synaptic_reconstruction.inference import segment_vesicles
from parse_table import parse_table

VERSIONS = {
    1: {
        "model": "/scratch/projects/nim00007/data/synaptic_reconstruction/models/moser/vesicles/mean-teacher-v3.zip",
        "distance_based_segmentation": True,
    },
}


def segment_folder(model_path, folder, version, is_new):
    if is_new:
        # This is the difference in scale between the new and old tomogram.
        scale = 1.47
    else:
        scale = None

    output_folder = os.path.join(folder, "automatisch", f"v{version}")
    os.makedirs(output_folder, exist_ok=True)

    data_path = get_data_path(folder)

    output_path = os.path.join(
        output_folder, Path(data_path).stem + "_vesicles.h5"
    )
    if os.path.exists(output_path):
        return

    print("Segmenting vesicles for", data_path)
    with open_file(data_path, "r") as f:
        data = f["data"][:]

    segmentation = segment_vesicles(
        data, model_path, verbose=False,
        distance_based_segmentation=VERSIONS[version]["distance_based_segmentation"],
        scale=scale,
    )
    with open_file(output_path, "a") as f:
        f.create_dataset("segmentation", data=segmentation, compression="gzip")


def run_vesicle_segmentation(table, version, process_new_microscope):
    model_path = VERSIONS[version]["model"]

    for i, row in tqdm(table.iterrows(), total=len(table)):
        folder = row["Local Path"]
        if folder == "":
            continue

        micro = row["EM alt vs. Neu"]
        if micro == "beides":
            segment_folder(model_path, folder, version, is_new=False)
            if process_new_microscope:
                folder_new = os.path.join("Tomo neues EM")
                segment_folder(model_path, folder_new, version, is_new=True)
        elif micro == "alt":
            segment_folder(model_path, folder, version, is_new=False)
        elif micro == "neu" and process_new_microscope:
            segment_folder(model_path, folder, version, is_new=True)


def main():
    table_path = "./Übersicht.xlsx"
    data_root = "/scratch-emmy/usr/nimcpape/data/moser"
    table = parse_table(table_path, data_root)

    version = 1
    process_new_microscope = False

    run_vesicle_segmentation(table, version, process_new_microscope)


if __name__ == "__main__":
    main()