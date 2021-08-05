# TODO: transform bounding box into sensor frame at time
# TODO: project 3D bounding box onto 2D visualization (radar/lidar top-down, camera front-face)
# TODO: render visualization as an image, (display it), (save it)
# TODO: plot odometry results vs. ground truth

import sys
import json
import glob
from collections import OrderedDict
from os import path
import csv
import copy
from math import sin, cos, pi
from threading import Lock

import cv2
import open3d as o3d
import open3d.ml.torch as ml3d
import matplotlib
import matplotlib.pyplot as plt
from matplotlib import cm
from matplotlib.widgets import Button
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
import tkinter as tk
import numpy as np
from scipy.spatial.transform import Rotation as R
from tqdm import tqdm

import vis_utils
import map_utils

matplotlib.use("tkagg")  # slu: for testing with ide

class LidarPose:
    def __init__(self, ros_ts, gps_ts, position, heading):
        self.ros_ts = ros_ts
        self.gps_ts = gps_ts
        self.position = position
        self.heading = heading  # roll, pitch, heading(yaw)

    def get_C_v_enu(self):
        return R.from_euler('xyz', self.heading)

class GPSPose:
    def __init__(self, gps_ts, position, heading):
        self.gps_ts = gps_ts
        self.position = position
        self.heading = heading  # roll, pitch, heading(yaw)

    def get_C_vo(self):
        return R.from_euler('xyz', self.heading)

class BoreasVisualizer:
    """Main class for loading the Boreas dataset for visualization.

    Loads relevant data, transforms, and labels and provides access to several visualization options.
    Currently only works for one track at a time.
    """

    def __init__(self, dataroot, ts_to_load=None):
        """Initialize the class with the corresponding data, transforms and labels.

        Args:
            dataroot: Path to the directory where the dataset is stored
        """
        # Check if dataroot paths are valid
        if not path.exists(path.join(dataroot, "camera")):
            raise ValueError("Error: images dir missing from dataroot")
        if not path.exists(path.join(dataroot, "lidar")):
            raise ValueError("Error: lidar dir missing from dataroot")
        if not path.exists(path.join(dataroot, "applanix")):
            raise ValueError("Error: applnix dir missing from dataroot")
        if not path.exists(path.join(dataroot, "calib")):
            raise ValueError("Error: calib dir missing from dataroot")
        # if not path.exists(path.join(dataroot, "labels.json")):
        #     raise ValueError("Error: labels.json missing from dataroot")

        # Instantiate class properties
        self.dataroot = dataroot  # Root directory for the dataset
        self.pcd_paths = sorted(glob.glob(path.join(dataroot, "lidar", "*.bin")))[0:ts_to_load]  # Paths to the pointcloud jsons
        self.img_paths = sorted(glob.glob(path.join(dataroot, "camera", "*.png")))[0:ts_to_load]  # Paths to the camera images
        self.label_file = path.join(dataroot, "labels.json")  # Path to the label json
        self.timestamps = []                        # List of all timestamps (in order)
        self.lidar_data = []                        # List of all loaded lidar jsons (in order)
        self.lidar_poses = {}                       # Dict of all the lidar poses (by ros timestamp)
        self.gps_poses = {}                         # Dict of all the gps poses (by GPS time)
        self.images_raw = []                        # List of all loaded cv2 images (in order, not 1-1 with timestamps)
        self.images_synced = []                     # List of all synced images (in order)
        self.labels = []                            # List of all loaded label jsons (in order)
        self.track_length = len(self.pcd_paths)     # Length of current track

        # Load transforms
        self.P_cam, self.T_iv, self.T_cv = vis_utils.get_sensor_calibration_alt("boreas",
                                                                            verbose=False)
        self.C_enu_ned = np.array([
            [0, 1, 0],
            [1, 0, 0],
            [0, 0, -1]
        ])

        # Load pointcloud data & timestamps
        print("Loading Lidar Pointclouds...")
        for pcd_path in tqdm(self.pcd_paths, file=sys.stdout):
            self.timestamps.append(int(pcd_path.split("/")[-1][:-4]))
            scan = np.fromfile(pcd_path, dtype=np.float32)
            points = scan.reshape((-1, 6))[:, :6]
            self.lidar_data.append(points)  # x, y, z, i, laser #, gps timestamp
        # Load lidar poses
        print("Loading Lidar Poses...")
        with open(path.join(self.dataroot, "applanix", "lidar_poses.csv")) as file:
            reader = csv.reader(file)
            headers = next(reader)  # Extract headers
            for row in tqdm(reader, file=sys.stdout):
                lidar_pose = LidarPose(int(row[0]), float(row[1]), np.asarray(row[2:5], dtype=np.float32), np.asarray(row[8:11], dtype=np.float32))
                self.lidar_poses[int(row[0])] = lidar_pose
        # Load gps poses
        print("Loading GPS Poses...")
        with open(path.join(self.dataroot, "applanix", "gps_post_process.csv")) as file:
            reader = csv.reader(file)
            headers = next(reader)  # Extract headers
            for row in tqdm(reader, file=sys.stdout):
                gps_pose = GPSPose(float(row[0]), np.asarray(row[1:4], dtype=np.float32), np.asarray(row[7:10], dtype=np.float32))
                self.gps_poses[float(row[0])] = gps_pose
        # Load camera data
        print("Loading Images...")
        for img_path in tqdm(self.img_paths, file=sys.stdout):
            self.images_raw.append(cv2.imread(img_path, cv2.IMREAD_COLOR))
        self._sync_camera_frames()  # Sync each lidar frame to a corresponding camera frame

        # # Load label data
        # print("Loading Labels...", flush=True)
        # with open(self.label_file, 'r') as file:
        #     raw_labels = json.load(file)
        #     for label in tqdm(raw_labels):
        #         self.labels.append(label['cuboids'])

        # For plot stuff
        self.curr_ts_idx = 0
        self.fig = None
        self.ax = None
        self.plot_update_mutex = Lock()


    def visualize_track_topdown_o3d(self):
        pc_data = []
        # bb_data = []

        for i in range(self.track_length):
            curr_lidar_data = self.lidar_data[i]
            curr_lables = self.labels[i]

            points, boxes = vis_utils.transform_data_to_sensor_frame(curr_lidar_data, curr_lables)
            points = points.astype(np.float32)

            frame_data = {
                'name': 'lidar_points/frame_{}'.format(i),
                'points': points
            }

            # bbox = ml3d.vis.BoundingBox3D()

            pc_data.append(frame_data)

        # Open3d ML Visualizer
        vis = ml3d.vis.Visualizer()
        vis.visualize(pc_data)
        vis.show_geometries_under("task", True)

    def export_video_topdown(self):
        imgs = []
        # Render the matplotlib figs to images
        print("Exporting Topdown View to Video")
        for i in tqdm(range(len(self.timestamps)), file=sys.stdout):
            self.visualize_track_topdown_mpl(frame_idx=i, show=False)
            canvas = FigureCanvas(self.fig)
            canvas.draw()
            graph_image = np.array(self.fig.canvas.get_renderer()._renderer)
            graph_image = cv2.cvtColor(graph_image, cv2.COLOR_RGB2BGR)
            imgs.append(graph_image)

        # Write the images to video
        out = cv2.VideoWriter('testing.avi', cv2.VideoWriter_fourcc(*'DIVX'), 15, (700, 700))
        for i in range(len(imgs)):
            out.write(imgs[i])
        out.release()

    def visualize_track_topdown_mpl(self, frame_idx, predictions=None, show=True):
        self.curr_ts_idx = frame_idx
        curr_ts = self.timestamps[frame_idx]
        curr_lidar_data = self.lidar_data[frame_idx][:]
        curr_lidar_pose = self.lidar_poses[curr_ts]
        # curr_lables = self.labels[frame_idx]

        self.fig, self.ax = plt.subplots(figsize=(7,7))

        button_ax = plt.axes([0.05, 0.05, 0.05, 0.05])
        button_f = Button(button_ax, "<")
        button_f.on_clicked(self.on_click_bkwd)

        button_ax2 = plt.axes([0.90, 0.05, 0.05, 0.05])
        button_f2 = Button(button_ax2, ">")
        button_f2.on_clicked(self.on_click_fwd)

        self.update_plot_topdown(self.ax, curr_lidar_data, curr_lidar_pose)

        if show:
            plt.show()
            plt.draw()
        else:
            plt.close(self.fig)

    def on_click_fwd(self, event):
        if not self.plot_update_mutex.acquire(timeout=0.5): return

        try:
            self.curr_ts_idx = min(self.curr_ts_idx + 1, len(self.timestamps) - 1)
            print("Visualizing Timestep Index: {}/{}".format(self.curr_ts_idx, len(self.timestamps)))

            self.ax.clear()
            curr_ts = self.timestamps[self.curr_ts_idx]
            curr_lidar_data = self.lidar_data[self.curr_ts_idx][:, :]
            curr_lidar_pose = self.lidar_poses[curr_ts]

            self.update_plot_topdown(self.ax, curr_lidar_data, curr_lidar_pose)

            print("Done")
        finally:
            self.plot_update_mutex.release()

    def on_click_bkwd(self, event):
        if not self.plot_update_mutex.acquire(timeout=0.5): return

        try:
            self.curr_ts_idx = max(self.curr_ts_idx - 1, 0)
            print("Visualizing Timestep Index: {}/{}".format(self.curr_ts_idx, len(self.timestamps)))

            self.ax.clear()
            curr_ts = self.timestamps[self.curr_ts_idx]
            curr_lidar_data = self.lidar_data[self.curr_ts_idx][:, :]
            curr_lidar_pose = self.lidar_poses[curr_ts]

            self.update_plot_topdown(self.ax, curr_lidar_data, curr_lidar_pose)

            print("Done")
        finally:
            self.plot_update_mutex.release()

    def update_plot_topdown(self, ax, lidar_data, lidar_pose):
        # Calculate transformations for current data
        C_v_enu = lidar_pose.get_C_v_enu().as_matrix()
        C_i_enu = self.T_iv[0:3, 0:3] @ C_v_enu
        C_iv = self.T_iv[0:3, 0:3]

        # Draw map
        map_utils.draw_map_without_lanelet("./sample_boreas/boreas_lane.osm", ax, lidar_pose.position[0], lidar_pose.position[1], C_i_enu, utm=True)

        # Calculate point colors
        z_min = -3
        z_max = 5
        colors = cm.jet(((lidar_data[:, 2] - z_min) / (z_max - z_min)) + 0.2, 1)[:, 0:3]

        # Draw lidar points
        pcd_i = np.matmul(C_iv[0:2, 0:2].reshape(1, 2, 2), lidar_data[:, 0:2].reshape(lidar_data.shape[0], 2, 1)).squeeze(-1)
        self.scatter = ax.scatter(pcd_i[:, 0], pcd_i[:, 1], color=colors, s=0.05)

        # Draw predictions (TODO)
        # for box in boxes:
        #     box.render_bbox_2d(ax)
        #
        # if predictions is not None:
        #     for box in predictions:
        #         box.render_bbox_2d(ax, color="k")

        # Set to scale labeling bounds
        self.ax.set_xlim(-75, 75)
        self.ax.set_ylim(-75, 75)

        plt.draw()

    def get_cam2vel_transform(self, pcd):
        pcd = np.matmul(self.T_cv, pcd)
        return pcd

    def visualize_frame_persp(self, frame_idx):
        for i in tqdm(range(frame_idx, len(self.timestamps))):
            points = self.lidar_data[i][:, 0:3]
            points = points[np.random.choice(len(points), int(0.5*len(points)), replace=False)]
            points = points.T
            points = np.vstack((points, np.ones(points.shape[1])))
            image = copy.deepcopy(self.images_synced[i])

            points_camera_all = self.get_cam2vel_transform(points)
            points_camera = np.array([])
            for i in range(points_camera_all.shape[1]):
                if points_camera_all[2,i] > 0:
                    points_camera = np.concatenate((points_camera, points_camera_all[:,i]))
            points_camera = np.reshape(points_camera, (-1,4)).T
            pixel_camera = np.matmul(self.P_cam, points_camera)

            max_z = int(max(pixel_camera[2,:])/3)
            for i in range(pixel_camera.shape[1]):
                z = pixel_camera[2,i]
                x = int(pixel_camera[0,i] / z)
                y = int(pixel_camera[1,i] / z)
                if x > 0 and x < image.shape[1] and y > 0 and y < image.shape[0]:
                    c = cv2.applyColorMap(np.array([int(pixel_camera[2,i] / max_z*255)], dtype=np.uint8), cv2.COLORMAP_RAINBOW).squeeze().tolist()
                    cv2.circle(image,(x,y), 1, c, 1)

            cv2.destroyAllWindows()
            cv2.imshow("Image " + str(i), image)
            cv2.waitKey(100)

    def _sync_camera_frames(self):
        # Helper function for finding closest timestamp
        def get_closest_ts(query_time, targets):
            min_delta = 1e33  # Temp set to this, should be 1e9
            closest = -1
            for i in range(len(targets)):
                delta = abs(query_time - targets[i])
                if delta < min_delta:
                    min_delta = delta
                    closest = i
            assert (closest >= 0), "closest time to query: {} in rostimes not found.".format(query_time)
            return closest, targets[closest]

        # Find closest lidar timestamp for each camera frame
        camera_timestamps = [int(f.replace('/', '.').split('.')[-2]) for f in self.img_paths]
        for i in range(self.track_length):
            timestamp = self.timestamps[i]
            corrected_timestamp = timestamp + vis_utils.get_dataset_offset_camera_ts("boreas")
            closet_idx, cloest_val = get_closest_ts(corrected_timestamp, camera_timestamps)
            self.images_synced.append(self.images_raw[closet_idx])


if __name__ == '__main__':
    ts_to_load=100
    dataset = BoreasVisualizer("./sample_boreas", ts_to_load)
    # dataset.visualize_track_topdown()
    # dataset.visualize_track_topdown_mpl(0)

    dataset.visualize_frame_persp(0)
