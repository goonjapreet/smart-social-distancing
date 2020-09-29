import base64
import time
from multiprocessing.managers import BaseManager
from fastapi import FastAPI, status, Request
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import cv2 as cv
import uvicorn
import os
import logging
import humps
import numpy as np
from starlette.exceptions import HTTPException
from pydantic import conlist
from pathlib import Path
from functools import reduce

from api.models.config_keys import *
from api.reports import reports_api
from share.commands import Commands

logger = logging.getLogger(__name__)


class QueueManager(BaseManager): pass


class ProcessorAPI:
    """
    The ProcessorAPI object implements a fastapi application that should allow configuring, starting and stopping processing,
    and viewing the video stream processed by this processor node.

    :param config: Is a ConfigEngine instance which provides necessary parameters.
    :param engine_instance:  A ConfigEngine object which store all of the config parameters. Access to any parameter
        is possible by calling get_section_dict method.
    """

    def __init__(self, config):
        self.config = config
        self._setup_queues()
        self._host = self.config.get_section_dict("API")["Host"]
        self._port = int(self.config.get_section_dict("API")["Port"])
        self._screenshot_directory = self.config.get_section_dict("App")["ScreenshotsDirectory"]
        self.app = self.create_fastapi_app()

    def _setup_queues(self):
        QueueManager.register('get_cmd_queue')
        QueueManager.register('get_result_queue')
        self._queue_host = self.config.get_section_dict("CORE")["Host"]
        self._queue_port = int(self.config.get_section_dict("CORE")["QueuePort"])
        auth_key = self.config.get_section_dict("CORE")["QueueAuthKey"]
        self._queue_manager = QueueManager(address=(self._queue_host, self._queue_port),
                                           authkey=auth_key.encode('ascii'))

        while True:
            try:
                self._queue_manager.connect()
                break
            except ConnectionRefusedError:
                logger.warning("Waiting for core's queue to initiate ... ")
                time.sleep(1)

        logger.info("Connection established to Core's queue")
        self._cmd_queue = self._queue_manager.get_cmd_queue()
        self._result_queue = self._queue_manager.get_result_queue()

    def create_fastapi_app(self):
        os.environ['LogDirectory'] = self.config.get_section_dict("Logger")["LogDirectory"]
        os.environ['HeatmapResolution'] = self.config.get_section_dict("Logger")["HeatmapResolution"]

        class ImageModel(BaseModel):
            image: str

            class Config:
                schema_extra = {
                    'example': {
                        'image': 'data:image/jpg;base64,iVBORw0KG...'
                    }
                }

        class ConfigHomographyMatrix(BaseModel):
            pts_source: conlist(conlist(float, min_items=2, max_items=2), min_items=4, max_items=4)
            pts_destination: conlist(conlist(float, min_items=2, max_items=2), min_items=4, max_items=4)

            class Config:
                schema_extra = {
                    'example': {
                        'pts_source': [[0., 0.], [0., 1.], [1., 1.], [1., 0.]],
                        'pts_destination': [[130., 310.], [45., 420.], [275., 420.], [252., 310.]]
                    }
                }

        # Create and return a fastapi instance
        app = FastAPI()

        app.mount("/reports", reports_api)

        @app.exception_handler(RequestValidationError)
        async def validation_exception_handler(request: Request, exc: RequestValidationError):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=jsonable_encoder({"detail": exc.errors(), "body": exc.body}),
            )

        if os.environ.get('DEV_ALLOW_ALL_ORIGINS', False):
            # This option allows React development server (which is served on another port, like 3000) to proxy requests
            # to this server.
            # WARNING: read this before enabling it in your environment:
            # https://medium.com/@stestagg/stealing-secrets-from-developers-using-websockets-254f98d577a0
            from fastapi.middleware.cors import CORSMiddleware
            app.add_middleware(CORSMiddleware, allow_origins='*', allow_credentials=True, allow_methods=['*'],
                               allow_headers=['*'])

        app.mount("/static", StaticFiles(directory="/repo/data/processor/static"), name="static")

        def map_camera(camera_name, config, options):
            camera = config.get(camera_name)
            camera_id = camera.get("Id")
            image = None
            if "withImage" in options:
                dir_path = os.path.join(self.config.get_section_dict("App")["ScreenshotsDirectory"], camera_id)
                image = base64.b64encode(cv.imread(f'{dir_path}/default.jpg'))

            return {
                "id": camera_id,
                "name": camera.get("Name"),
                "videoPath": camera.get("VideoPath"),
                "emails": camera.get("Emails"),
                "violationThreshold": camera.get("ViolationThreshold"),
                "notifyEveryMinutes": camera.get("NotifyEveryMinutes"),
                "image": image
            }

        def map_config(config, options):
            cameras_name = [x for x in config.keys() if x.startswith("Source")]
            return {
                "host": config.get("API").get("Host"),
                "port": config.get("API").get("Port"),
                "cameras": [map_camera(x, config, options) for x in cameras_name]
            }

        def map_to_config_file_format(config_dto):
            config_dict = dict()
            for count, camera in enumerate(config_dto.cameras):
                config_dict["Source_" + str(count)] = dict(
                    {
                        'Name': camera.name,
                        'VideoPath': camera.videoPath,
                        'Id': camera.id,
                        'Emails': camera.emails,
                        'Tags': camera.tags,
                        'NotifyEveryMinutes': str(camera.notifyEveryMinutes),
                        'ViolationThreshold': str(camera.violationThreshold),
                        'DistMethod': camera.distMethod,
                        'CalibrationFile': camera.calibrationFile
                    }
                )
            return config_dict

        def verify_path(base, camera_id):
            dir_path = os.path.join(base, camera_id)
            if not os.path.exists(dir_path):
                raise HTTPException(status_code=404, detail=f'Camera with id "{camera_id}" does not exist')
            return dir_path

        def update_config_file(config_dict):
            logger.info("Updating config...")
            self.config.update_config(config_dict)
            self.config.reload()

        def restart_processor():
            logger.info("Restarting video processor...")
            self._cmd_queue.put(Commands.STOP_PROCESS_VIDEO)
            stopped = self._result_queue.get()
            if stopped:
                self._cmd_queue.put(Commands.PROCESS_VIDEO_CFG)
                started = self._result_queue.get()
                if not started:
                    logger.info("Failed to restart video processor...")
                    return False
            return True

        def compute_and_save_inv_homography_matrix(points: ConfigHomographyMatrix, destination: str):
            Path(os.path.dirname(destination)).mkdir(parents=True, exist_ok=True)
            h, _ = cv.findHomography(np.float32(points.pts_source), np.float32(points.pts_destination))
            h_inv = np.linalg.inv(h).flatten()
            h_inv = ' '.join(map(str, h_inv))
            with open(destination, 'w') as f:
                f.write('h_inv: ' + h_inv)

        @app.get("/process-video-cfg")
        async def process_video_cfg():
            logger.info("process-video-cfg requests on api")
            self._cmd_queue.put(Commands.PROCESS_VIDEO_CFG)
            logger.info("waiting for core's response...")
            result = self._result_queue.get()
            return result

        @app.get("/stop-process-video")
        async def stop_process_video():
            logger.info("stop-process-video requests on api")
            self._cmd_queue.put(Commands.STOP_PROCESS_VIDEO)
            logger.info("waiting for core's response...")
            result = self._result_queue.get()
            return result

        @app.get("/config", response_model=ConfigDTO)
        async def get_config(options: Optional[str] = ""):
            logger.info("get-config requests on api")
            sections = self.config.get_sections()
            config = {}
            for section in sections:
                config[section] = self.config.get_section_dict(section)
            return map_config(config, options)

        @app.put("/config")
        async def update_config(config: ConfigDTO):
            config_dict = map_to_config_file_format(config)
            update_config_file(config_dict)
            # TODO: Restart only when necessary, and only the threads that are necessary (for instance to load a new video)
            success = restart_processor()
            if not success:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=jsonable_encoder({
                        'msg': 'Failed to restart video processor',
                        'type': 'unknown error on the config file',
                        'body': humps.decamelize(config)
                    })
                )
            return JSONResponse(content=humps.decamelize(config))

        @app.get("/{camera_id}/image", response_model=ImageModel)
        async def get_camera_image(camera_id):
            dir_path = verify_path(self.config.get_section_dict("App")["ScreenshotsDirectory"], camera_id)
            with open(f'{dir_path}/default.jpg', "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read())
            return {
                "image": encoded_string
            }

        @app.put("/{camera_id}/image")
        async def replace_camera_image(camera_id, body: ImageModel):
            dir_path = verify_path(self.config.get_section_dict("App")["ScreenshotsDirectory"], camera_id)
            try:
                decoded_image = base64.b64decode(body.image.split(',')[1])
                nparr = np.fromstring(decoded_image, np.uint8)
                cv_image = cv.imdecode(nparr, cv.IMREAD_COLOR)
                cv.imwrite(f"{dir_path}/default.jpg", cv_image)
            except Exception:
                return HTTPException(status_code=400, detail="Invalid image format")

        @app.post("/{camera_id}/homography_matrix")
        async def config_calibrated_distance(camera_id, body: ConfigHomographyMatrix):
            sources = self.config.get_video_sources()
            dir_source = list(filter(lambda source: source['id'] == camera_id, sources))
            if dir_source is None or len(dir_source) != 1:
                raise HTTPException(status_code=404, detail=f'Camera with id "{camera_id}" does not exist')
            dir_source = dir_source[0]
            dir_path = dir_source['calibration_file']
            compute_and_save_inv_homography_matrix(points=body, destination=dir_path)

            sections = self.config.get_sections()
            config_dict = {}
            for section in sections:
                config_dict[section] = self.config.get_section_dict(section)
            config_dict[dir_source['section']]['DistMethod'] = 'CalibratedDistance'
            update_config_file(config_dict)
            success = restart_processor()
            if not success:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=jsonable_encoder({
                        'msg': 'Failed to restart video processor',
                        'type': 'unknown error on the config file',
                        'body': humps.decamelize(config_dict)
                    })
                )
            return JSONResponse(content=humps.decamelize(config_dict))

        return app

    def start(self):
        uvicorn.run(self.app, host=self._host, port=self._port, log_level='info', access_log=False)
