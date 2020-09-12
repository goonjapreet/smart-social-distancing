import cv2 as cv
import numpy as np
from scipy.spatial.distance import cdist
import math
import os
import datetime
import shutil
from libs.centroid_object_tracker import CentroidTracker
from libs.loggers.loggers import Logger
from tools.environment_score import mx_environment_scoring_consider_crowd
from tools.objects_post_process import extract_violating_objects
from libs.utils import visualization_utils
import logging

logger = logging.getLogger(__name__)


class Distancing:

    def __init__(self, config):
        self.config = config
        self.detector = None
        self.device = self.config.get_section_dict('Detector')['Device']
        self.running_video = False
        self.tracker = CentroidTracker(
            max_disappeared=int(self.config.get_section_dict("PostProcessor")["MaxTrackFrame"]))
        self.logger = Logger(self.config)
        self.image_size = [int(i) for i in self.config.get_section_dict('Detector')['ImageSize'].split(',')]

        self.dist_method = self.config.get_section_dict("PostProcessor")["DistMethod"]
        self.dist_threshold = self.config.get_section_dict("PostProcessor")["DistThreshold"]
        self.resolution = tuple([int(i) for i in self.config.get_section_dict('App')['Resolution'].split(',')])
        self.birds_eye_resolution = (200, 300)
        if self.dist_method == "CalibratedDistance":
            try:
                calibration_file = self.config.get_section_dict("App")["CalibrationFile"]
            except KeyError:
                raise ValueError(
                    "The 'CalibrationFile' should be specified in config file in case of using 'CalibratedDistance' method")
            try: 
                with open(calibration_file, "r") as file:
                    self.h_inv = file.readlines()[0].split(" ")[1:]
                    self.h_inv = np.array(self.h_inv, dtype="float").reshape((3, 3))
            except FileNotFoundError:
                raise FileNotFoundError("The specified 'CalibrationFile' does not exist")

    def __process(self, cv_image):
        """
        return object_list list of  dict for each obj,
        obj["bbox"] is normalized coordinations for [x0, y0, x1, y1] of box
        """

        # Resize input image to resolution
        cv_image = cv.resize(cv_image, self.resolution)

        resized_image = cv.resize(cv_image, tuple(self.image_size[:2]))
        rgb_resized_image = cv.cvtColor(resized_image, cv.COLOR_BGR2RGB)
        tmp_objects_list = self.detector.inference(rgb_resized_image)
        [w, h] = self.resolution

        for obj in tmp_objects_list:
            box = obj["bbox"]
            x0 = box[1]
            y0 = box[0]
            x1 = box[3]
            y1 = box[2]
            obj["centroid"] = [(x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0]
            obj["bbox"] = [x0, y0, x1, y1]
            obj["centroidReal"] = [(x0 + x1) * w / 2, (y0 + y1) * h / 2, (x1 - x0) * w, (y1 - y0) * h]
            obj["bboxReal"] = [x0 * w, y0 * h, x1 * w, y1 * h]

        objects_list, distancings = self.calculate_distancing(tmp_objects_list)
        return cv_image, objects_list, distancings

    def gstreamer_writer(self, feed_name, fps, resolution):
        """
        This method creates and returns an OpenCV Video Writer instance. The VideoWriter expects its `.write()` method
        to be called with a single frame image multiple times. It encodes frames into live video segments and produces
        a video segment once it has received enough frames to produce a 5-seconds segment of live video.
        The video segments are written on the filesystem. The target directory for writing segments is determined by
        `video_root` variable.  In addition to writing the video segments, the VideoWriter also updates a file named
        playlist.m3u8 in the target directory. This file contains the list of generated video segments and is updated
        automatically.
        This instance does not serve these video segments to the client. It is expected that the target video directory
        is being served by a static file server and the clientside HLS video library downloads "playlist.m3u8". Then,
        the client video player reads the link for video segments, according to HLS protocol, and downloads them from
        static file server.

        :param feed_name: Is the name for video feed. We may have multiple cameras, each with multiple video feeds (e.g. one
        feed for visualizing bounding boxes and one for bird's eye view). Each video feed should be written into a
        separate directory. The name for target directory is defined by this variable.
        :param fps: The HLS video player on client side needs to know how many frames should be shown to the user per
        second. This parameter is independent from the frame rate with which the video is being processed. For example,
        if we set fps=60, but produce only frames (by calling `.write()`) per second, the client will see a loading
        indicator for 5*60/30 seconds and then 5 seconds of video is played with fps 60.
        :param resolution: A tuple of size 2 which indicates the resolution of output video.
        """
        encoder = self.config.get_section_dict('App')['Encoder']
        video_root = f'/repo/data/web_gui/static/gstreamer/{feed_name}'

        shutil.rmtree(video_root, ignore_errors=True)
        os.makedirs(video_root, exist_ok=True)

        playlist_root = f'/static/gstreamer/{feed_name}'
        if not playlist_root.endswith('/'):
            playlist_root = f'{playlist_root}/'
        # the entire encoding pipeline, as a string:
        pipeline = f'appsrc is-live=true !  {encoder} ! mpegtsmux ! hlssink max-files=15 ' \
                   f'target-duration=5 ' \
                   f'playlist-root={playlist_root} ' \
                   f'location={video_root}/video_%05d.ts ' \
                   f'playlist-location={video_root}/playlist.m3u8 '

        out = cv.VideoWriter(
            pipeline,
            cv.CAP_GSTREAMER,
            0, fps, resolution
        )

        if not out.isOpened():
            raise RuntimeError("Could not open gstreamer output for " + feed_name)
        return out

    def process_video(self, video_uri):
        if self.device == 'Jetson':
            from libs.detectors.jetson.detector import Detector
            self.detector = Detector(self.config)
        elif self.device == 'EdgeTPU':
            from libs.detectors.edgetpu.detector import Detector
            self.detector = Detector(self.config)
        elif self.device == 'Dummy':
            from libs.detectors.dummy.detector import Detector
            self.detector = Detector(self.config)
        elif self.device == 'x86':
            from libs.detectors.x86.detector import Detector
            self.detector = Detector(self.config)

        if self.device != 'Dummy':
            print('Device is: ', self.device)
            print('Detector is: ', self.detector.name)
            print('image size: ', self.image_size)

        input_cap = cv.VideoCapture(video_uri)
        fps = input_cap.get(cv.CAP_PROP_FPS)

        if (input_cap.isOpened()):
            logger.info(f'opened video {video_uri}')
        else:
            logger.error(f'failed to load video {video_uri}')
            return

        self.running_video = True

        # enable logging gstreamer Errors (https://stackoverflow.com/questions/3298934/how-do-i-view-gstreamer-debug-output)
        os.environ['GST_DEBUG'] = "*:1"
        out, out_birdseye = (
            self.gstreamer_writer(feed, fps, resolution)
            for (feed, resolution) in (
            ('default', self.resolution),
            ('default-birdseye', self.birds_eye_resolution)
        )  # TODO: use camera-id
        )

        dist_threshold = float(self.config.get_section_dict("PostProcessor")["DistThreshold"])
        class_id = int(self.config.get_section_dict('Detector')['ClassID'])
        frame_num = 0
        while input_cap.isOpened() and self.running_video:
            _, cv_image = input_cap.read()
            birds_eye_window = np.zeros(self.birds_eye_resolution[::-1] + (3,), dtype="uint8")
            if np.shape(cv_image) != ():
                cv_image, objects, distancings = self.__process(cv_image)
                output_dict = visualization_utils.visualization_preparation(objects, distancings, dist_threshold)

                category_index = {class_id: {
                    "id": class_id,
                    "name": "Pedestrian",
                }}  # TODO: json file for detector config
                # Draw bounding boxes and other visualization factors on input_frame
                visualization_utils.visualize_boxes_and_labels_on_image_array(
                    cv_image,
                    output_dict["detection_boxes"],
                    output_dict["detection_classes"],
                    output_dict["detection_scores"],
                    output_dict["detection_colors"],
                    category_index,
                    instance_masks=output_dict.get("detection_masks"),
                    use_normalized_coordinates=True,
                    line_thickness=3,
                )
                # TODO: Implement perspective view for objects
                birds_eye_window = visualization_utils.birds_eye_view(birds_eye_window, output_dict["detection_boxes"],
                                                                      output_dict["violating_objects"])
                try:
                    fps = self.detector.fps
                except:
                    # fps is not implemented for the detector instance"
                    fps = None

                # Put fps to the frame
                # region
                # -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_-
                txt_fps = 'Frames rate = ' + str(fps) + '(fps)'  # Frames rate = 95 (fps)
                # (0, 0) is the top-left (x,y); normalized number between 0-1
                origin = (0.05, 0.93)
                visualization_utils.text_putter(cv_image, txt_fps, origin)
                # -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_-
                # endregion

                # Put environment score to the frame
                # region
                # -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_-
                violating_objects = extract_violating_objects(distancings, dist_threshold)
                env_score = mx_environment_scoring_consider_crowd(len(objects), len(violating_objects))
                txt_env_score = 'Env Score = ' + str(env_score)  # Env Score = 0.7
                origin = (0.05, 0.98)
                visualization_utils.text_putter(cv_image, txt_env_score, origin)
                # -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_- -_-
                # endregion

                out.write(cv_image)
                out_birdseye.write(birds_eye_window)
                frame_num += 1
                if frame_num % 10 == 1:
                    logger.info(f'processed frame {frame_num} for {video_uri}')
            else:
                continue
            self.logger.update(objects, distancings)
        input_cap.release()
        out.release()
        out_birdseye.release()
        del self.detector
        self.running_video = False

    def stop_process_video(self):
        self.running_video = False

    def calculate_distancing(self, objects_list):
        """
        this function post-process the raw boxes of object detector and calculate a distance matrix
        for detected bounding boxes.
        post processing is consist of:
        1. omitting large boxes by filtering boxes which are biger than the 1/4 of the size the image.
        2. omitting duplicated boxes by applying an auxilary non-maximum-suppression.
        3. apply a simple object tracker to make the detection more robust.

        params:
        object_list: a list of dictionaries. each dictionary has attributes of a detected object such as
        "id", "centroid" (a tuple of the normalized centroid coordinates (cx,cy,w,h) of the box) and "bbox" (a tuple
        of the normalized (xmin,ymin,xmax,ymax) coordinate of the box)

        returns:
        object_list: the post processed version of the input
        distances: a NxN ndarray which i,j element is distance between i-th and l-th bounding box

        """
        new_objects_list = self.ignore_large_boxes(objects_list)
        new_objects_list = self.non_max_suppression_fast(new_objects_list,
                                                         float(self.config.get_section_dict("PostProcessor")[
                                                                   "NMSThreshold"]))
        tracked_boxes = self.tracker.update(new_objects_list)
        new_objects_list = [tracked_boxes[i] for i in tracked_boxes.keys()]
        for i, item in enumerate(new_objects_list):
            item["id"] = item["id"].split("-")[0] + "-" + str(i)

        centroids = np.array([obj["centroid"] for obj in new_objects_list])
        distances = self.calculate_box_distances(new_objects_list)

        return new_objects_list, distances

    @staticmethod
    def ignore_large_boxes(object_list):

        """
        filtering boxes which are biger than the 1/4 of the size the image
        params:
            object_list: a list of dictionaries. each dictionary has attributes of a detected object such as
            "id", "centroid" (a tuple of the normalized centroid coordinates (cx,cy,w,h) of the box) and "bbox" (a tuple
            of the normalized (xmin,ymin,xmax,ymax) coordinate of the box)
        returns:
        object_list: input object list without large boxes
        """
        large_boxes = []
        for i in range(len(object_list)):
            if (object_list[i]["centroid"][2] * object_list[i]["centroid"][3]) > 0.25:
                large_boxes.append(i)
        updated_object_list = [j for i, j in enumerate(object_list) if i not in large_boxes]
        return updated_object_list

    @staticmethod
    def non_max_suppression_fast(object_list, overlapThresh):

        """
        omitting duplicated boxes by applying an auxilary non-maximum-suppression.
        params:
        object_list: a list of dictionaries. each dictionary has attributes of a detected object such
        "id", "centroid" (a tuple of the normalized centroid coordinates (cx,cy,w,h) of the box) and "bbox" (a tuple
        of the normalized (xmin,ymin,xmax,ymax) coordinate of the box)

        overlapThresh: threshold of minimum IoU of to detect two box as duplicated.

        returns:
        object_list: input object list without duplicated boxes
        """
        # if there are no boxes, return an empty list
        boxes = np.array([item["centroid"] for item in object_list])
        corners = np.array([item["bbox"] for item in object_list])
        if len(boxes) == 0:
            return []
        if boxes.dtype.kind == "i":
            boxes = boxes.astype("float")
        # initialize the list of picked indexes
        pick = []
        cy = boxes[:, 1]
        cx = boxes[:, 0]
        h = boxes[:, 3]
        w = boxes[:, 2]
        x1 = corners[:, 0]
        x2 = corners[:, 2]
        y1 = corners[:, 1]
        y2 = corners[:, 3]
        area = (h + 1) * (w + 1)
        idxs = np.argsort(cy + (h / 2))
        while len(idxs) > 0:
            last = len(idxs) - 1
            i = idxs[last]
            pick.append(i)
            xx1 = np.maximum(x1[i], x1[idxs[:last]])
            yy1 = np.maximum(y1[i], y1[idxs[:last]])
            xx2 = np.minimum(x2[i], x2[idxs[:last]])
            yy2 = np.minimum(y2[i], y2[idxs[:last]])

            w = np.maximum(0, xx2 - xx1 + 1)
            h = np.maximum(0, yy2 - yy1 + 1)
            # compute the ratio of overlap
            overlap = (w * h) / area[idxs[:last]]
            # delete all indexes from the index list that have
            idxs = np.delete(idxs, np.concatenate(([last],
                                                   np.where(overlap > overlapThresh)[0])))
        updated_object_list = [j for i, j in enumerate(object_list) if i in pick]
        return updated_object_list

    def calculate_distance_of_two_points_of_boxes(self, first_point, second_point):

        """
        This function calculates a distance l for two input corresponding points of two detected bounding boxes.
        it is assumed that each person is H = 170 cm tall in real scene to map the distances in the image (in pixels) to 
        physical distance measures (in meters). 

        params:
        first_point: (x, y, h)-tuple, where x,y is the location of a point (center or each of 4 corners of a bounding box)
        and h is the height of the bounding box. 
        second_point: same tuple as first_point for the corresponding point of other box 

        returns:
        l:  Estimated physical distance (in centimeters) between first_point and second_point.


        """

        # estimate corresponding points distance
        [xc1, yc1, h1] = first_point
        [xc2, yc2, h2] = second_point

        dx = xc2 - xc1
        dy = yc2 - yc1

        lx = dx * 170 * (1 / h1 + 1 / h2) / 2
        ly = dy * 170 * (1 / h1 + 1 / h2) / 2

        l = math.sqrt(lx ** 2 + ly ** 2)

        return l

    def calculate_box_distances(self, nn_out):

        """
        This function calculates a distance matrix for detected bounding boxes.
        Three methods are implemented to calculate the distances, the first one estimates distance with a calibration matrix
        which transform the points to the 3-d world coordinate, the second one estimates distance of center points of the
        boxes and the third one uses minimum distance of each of 4 points of bounding boxes.

        params:
        object_list: a list of dictionaries. each dictionary has attributes of a detected object such as
        "id", "centroidReal" (a tuple of the centroid coordinates (cx,cy,w,h) of the box) and "bboxReal" (a tuple
        of the (xmin,ymin,xmax,ymax) coordinate of the box)

        returns:
        distances: a NxN ndarray which i,j element is estimated distance between i-th and j-th bounding box in real scene (cm)

        """ 
        if self.dist_method == "CalibratedDistance":
            world_coordinate_points = np.array([self.transform_to_world_coordinate(bbox) for bbox in nn_out])
            if len(world_coordinate_points) == 0:
                distances_asarray = np.array([])
            else:
                distances_asarray = cdist(world_coordinate_points, world_coordinate_points) 

        else:
            distances = []
            for i in range(len(nn_out)):
                distance_row = []
                for j in range(len(nn_out)):
                    if i == j:
                        l = 0
                    else:
                        if (self.dist_method == 'FourCornerPointsDistance'):
                            lower_left_of_first_box = [nn_out[i]["bboxReal"][0], nn_out[i]["bboxReal"][1],
                                                       nn_out[i]["centroidReal"][3]]
                            lower_right_of_first_box = [nn_out[i]["bboxReal"][2], nn_out[i]["bboxReal"][1],
                                                        nn_out[i]["centroidReal"][3]]
                            upper_left_of_first_box = [nn_out[i]["bboxReal"][0], nn_out[i]["bboxReal"][3],
                                                       nn_out[i]["centroidReal"][3]]
                            upper_right_of_first_box = [nn_out[i]["bboxReal"][2], nn_out[i]["bboxReal"][3],
                                                        nn_out[i]["centroidReal"][3]]

                            lower_left_of_second_box = [nn_out[j]["bboxReal"][0], nn_out[j]["bboxReal"][1],
                                                        nn_out[j]["centroidReal"][3]]
                            lower_right_of_second_box = [nn_out[j]["bboxReal"][2], nn_out[j]["bboxReal"][1],
                                                         nn_out[j]["centroidReal"][3]]
                            upper_left_of_second_box = [nn_out[j]["bboxReal"][0], nn_out[j]["bboxReal"][3],
                                                        nn_out[j]["centroidReal"][3]]
                            upper_right_of_second_box = [nn_out[j]["bboxReal"][2], nn_out[j]["bboxReal"][3],
                                                         nn_out[j]["centroidReal"][3]]

                            l1 = self.calculate_distance_of_two_points_of_boxes(lower_left_of_first_box,
                                                                                lower_left_of_second_box)
                            l2 = self.calculate_distance_of_two_points_of_boxes(lower_right_of_first_box,
                                                                                lower_right_of_second_box)
                            l3 = self.calculate_distance_of_two_points_of_boxes(upper_left_of_first_box,
                                                                                upper_left_of_second_box)
                            l4 = self.calculate_distance_of_two_points_of_boxes(upper_right_of_first_box,
                                                                                upper_right_of_second_box)

                            l = min(l1, l2, l3, l4)
                        elif (self.dist_method == 'CenterPointsDistance'):
                            center_of_first_box = [nn_out[i]["centroidReal"][0], nn_out[i]["centroidReal"][1],
                                                   nn_out[i]["centroidReal"][3]]
                            center_of_second_box = [nn_out[j]["centroidReal"][0], nn_out[j]["centroidReal"][1],
                                                    nn_out[j]["centroidReal"][3]]

                            l = self.calculate_distance_of_two_points_of_boxes(center_of_first_box, center_of_second_box)
                    distance_row.append(l)
                distances.append(distance_row)
            distances_asarray = np.asarray(distances, dtype=np.float32)
        return distances_asarray

    def transform_to_world_coordinate(self, bbox):
        """
        This function will transform the center of the bottom line of a bounding box from image coordinate to world
        coordinate via a homography matrix
        Args:
            bbox: a dictionary of a  coordinates of a detected instance with "id",
            "centroidReal" (a tuple of the centroid coordinates (cx,cy,w,h) of the box) and "bboxReal" (a tuple
            of the (xmin,ymin,xmax,ymax) coordinate of the box) keys

        Returns:
            A numpy array of (X,Y) of transformed point

        """
        floor_point = np.array([int((bbox["bboxReal"][0] + bbox["bboxReal"][2]) / 2), bbox["bboxReal"][3], 1])
        floor_world_point = np.matmul(self.h_inv, floor_point)
        floor_world_point = floor_world_point[:-1] / floor_world_point[-1]
        return floor_world_point
