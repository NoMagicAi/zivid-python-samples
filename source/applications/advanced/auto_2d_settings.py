"""
Automatically find 2D settings for a 2D capture by using a Zivid calibration board.

This sample uses the Zivid calibration board (ZVDA-CB01) to find settings (acquisition and color balance) for a 2D
capture automatically, given the ambient light in the scene. It calibrates against the white squares on the
checkerboard, trying to find settings so that the intensity values on the white squares are roughly the same as the
true whites of the checkerboard. The internal projector is by default turned OFF when finding 2D settings, but can
be turned on by setting a command line argument. Color balancing can optionally be turned off by setting a command
line argument if you only want to find acquisition settings.

Place the calibration board at the furthest or closest distance you want to image, and make sure the calibration board
is in view of the camera. Be aware that very low, very high or uneven ambient light may make it difficult to detect the
calibration board checkers and find good settings.

Change the steps in _adjust_acquisition_settings_2d() if you want to re-prioritize which acquisition settings to tune
first. If you want to use your own white reference (white wall, piece of paper, etc.) instead of using the calibration
board, you can provide your own mask in _main(). Then you will have to specify the lower limit for f-number yourself.

"""

import argparse
import time
from datetime import timedelta
from pathlib import Path
from typing import Tuple, Optional

import cv2
import matplotlib.pyplot as plt
import numpy as np
import zivid
import zivid.calibration
from zividsamples.calibration_board_utils import find_white_mask_from_checkerboard
from zividsamples.white_balance_calibration import (
    camera_may_need_color_balancing,
    compute_mean_rgb_from_mask,
    white_balance_calibration,
)


def _options() -> argparse.Namespace:
    """Configure and take command line arguments from user.

    Returns:
        Arguments from user

    """
    parser = argparse.ArgumentParser(
        description=(
            "Find 2D settings automatically with a Zivid calibration board\n"
            "Examples:\n"
            "\t1) $ python auto_2d_settings.py --desired-focus-range 500 --checkerboard-at-start-of-range\n"
            "\t2) $ python auto_2d_settings.py --desired-focus-range 500 --checkerboard-at-end-of-range\n"
            "\t3) $ python auto_2d_settings.py --desired-focus-range 500 --checkerboard-at-end-of-range --use-projector\n"
            "\t4) $ python auto_2d_settings.py --desired-focus-range 500 --checkerboard-at-end-of-range --no-color-balance\n\n"
            "In 1), the desired focus range starts at the checkerboard and goes 500mm away from the camera.\n"
            "In 2), the desired focus range ends at the checkerboard and goes 500mm towards the camera.\n"
            "In 3), the internal projector is also used with the 2D settings.\n"
            "In 4), color balancing is turned off.\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    # Required
    parser.add_argument(
        "--dfr",
        "--desired-focus-range",
        type=float,
        required=True,
        dest="desired_focus_range",
        help="Distance from checkerboard that should be in focus",
    )
    parser.add_argument(
        "--desired-white-range",
        type=float,
        nargs=2,
        required=True,
        dest="desired_white_range",
        help="Desired white range (min, max)",
    )

    checkerboard_group = parser.add_mutually_exclusive_group(required=True)
    checkerboard_group.add_argument(
        "--s",
        "--checkerboard-at-start-of-range",
        dest="checkerboard_at_start_of_range",
        help="Set checkerboard to closest imaging distance",
        action="store_true",
    )
    checkerboard_group.add_argument(
        "--e",
        "--checkerboard-at-end-of-range",
        dest="checkerboard_at_end_of_range",
        help="Set checkerboard to farthest imaging distance",
        action="store_true",
    )

    # Optional
    parser.add_argument(
        "--use-projector",
        dest="use_projector",
        help="Enable to use the internal projector with 2D settings",
        action="store_true",
    )
    parser.add_argument(
        "--no-color-balance",
        dest="find_color_balance",
        action="store_false",
        help="Enable to turn off color balancing",
    )
    parser.add_argument(
        "--pixel-sampling",
        dest="pixel_sampling",
        type=str,
        default="none",
        help="Pixel sampling for 2D settings, options supported by script: none, by2x2, by4x4",
        choices=["none", "by2x2", "by4x4"],
    )
    parser.add_argument(
        "--max-gain-override",
        dest="max_gain_override",
        type=float,
        default=None,
        help="Override the maximum gain used in the tuning process",
    )
    parser.add_argument(
        "--calibration-id",
        dest="calibration_id",
        type=str,
        default="",
        help="Calibration ID used for logging",
    )
    parser.add_argument(
        "--log-dir",
        dest="log_dir",
        type=Path,
        default=Path("/tmp/auto_2d_settings"),
        help="Directory to save log files and images",
    )

    return parser.parse_args()


def _get_current_time_ms() -> int:
    return int(time.time() * 1000)


def _capture_rgb(camera: zivid.Camera, settings_2d: zivid.Settings2D) -> np.ndarray:
    """Capture a 2D image and extract RGB values.

    Args:
        camera: Zivid camera
        settings_2d: Zivid 2D capture settings

    Returns:
        rgb: RGB image (H, W, 3)

    """
    rgb = camera.capture_2d(settings_2d).image_rgba_srgb().copy_data()[:, :, :3]
    return rgb


def _find_white_mask_and_distance_to_checkerboard(camera: zivid.Camera) -> Tuple[np.ndarray, np.ndarray, float]:
    """Generate a 2D mask of the white checkers on a checkerboard and calculate the distance to it.
       The capture is done with the camera's capture assistant settings, in full resolution.

    Args:
        camera: Zivid camera

    Raises:
        RuntimeError: If either cannot calculate pose or find checkerboard in image

    Returns:
        white_squares_mask: Mask of bools (H, W) for pixels containing white checkerboard squares
        distance_to_checkerboard: Translation in z from the camera to the checkerboard center

    """
    try:
        frame = zivid.calibration.capture_calibration_board(camera)
        checkerboard_pose = zivid.calibration.detect_calibration_board(frame).pose().to_matrix()
        distance_to_checkerboard = checkerboard_pose[2, 3]
        # TODO check why rgba_sgrb?
        rgb = frame.point_cloud().copy_data("rgba_sgrb")[:, :, :3]
        white_squares_mask = find_white_mask_from_checkerboard(rgb)

    except RuntimeError as exc:
        raise RuntimeError("Unable to find checkerboard, make sure it is in view of the camera.") from exc

    return rgb, white_squares_mask, distance_to_checkerboard


def _find_lowest_acceptable_fnum(camera: zivid.Camera, image_distance_near: float, image_distance_far: float) -> float:
    """Find the lowest f-number that gives a focused image, using the camera model and desired focus range.

    Args:
        camera: Zivid camera
        image_distance_near: Closest distance from camera that should be in focus
        image_distance_far: Furthest distance from camera that should be in focus

    Raises:
        RuntimeError: If camera model is not supported

    Returns:
        Lowest acceptable f-number that gives a focused image

    """
    if camera.info.model == zivid.CameraInfo.Model.zividTwo:
        focus_distance = 700
        focal_length = 8
        circle_of_confusion = 0.015
        fnum_min = 1.8
        if image_distance_near < 300 or image_distance_far > 1300:
            print(
                f"WARNING: Closest imaging distance ({image_distance_near:.2f}) or farthest imaging distance"
                f"({image_distance_far:.2f}) is outside recommended working distance for camera [300, 1300]"
            )
    elif camera.info.model == zivid.CameraInfo.Model.zividTwoL100:
        focus_distance = 1000
        focal_length = 8
        circle_of_confusion = 0.015
        fnum_min = 1.8
        if image_distance_near < 600 or image_distance_far > 1600:
            print(
                f"WARNING: Closest imaging distance ({image_distance_near:.2f}) or farthest imaging distance"
                f"({image_distance_far:.2f}) is outside recommended working distance for camera [600, 1600]"
            )
    elif camera.info.model in (zivid.CameraInfo.Model.zivid2PlusM130, zivid.CameraInfo.Model.zivid2PlusMR130):
        focus_distance = 1300
        focal_length = 11
        circle_of_confusion = 0.008
        fnum_min = 2.1
        if image_distance_near < 800 or image_distance_far > 2000:
            print(
                f"WARNING: Closest imaging distance ({image_distance_near:.2f}) or farthest imaging distance"
                f"({image_distance_far:.2f}) is outside recommended working distance for camera [800, 2000]"
            )
    elif camera.info.model in (zivid.CameraInfo.Model.zivid2PlusM60, zivid.CameraInfo.Model.zivid2PlusMR60):
        focus_distance = 600
        focal_length = 6.75
        circle_of_confusion = 0.008
        fnum_min = 2.37
        if image_distance_near < 300 or image_distance_far > 1100:
            print(
                f"WARNING: Closest imaging distance ({image_distance_near:.2f}) or farthest imaging distance"
                f"({image_distance_far:.2f}) is outside recommended working distance for camera [300, 1100]"
            )
    elif camera.info.model in (zivid.CameraInfo.Model.zivid2PlusL110, zivid.CameraInfo.Model.zivid2PlusLR110):
        focus_distance = 1100
        focal_length = 6.75
        circle_of_confusion = 0.008
        fnum_min = 2.37
        if image_distance_near < 800 or image_distance_far > 2000:
            print(
                f"WARNING: Closest imaging distance ({image_distance_near:.2f}) or farthest imaging distance"
                f"({image_distance_far:.2f}) is outside recommended working distance for camera [700, 1700]"
            )
    else:
        raise RuntimeError("Unsupported camera model in this sample.")

    fnum_near = (
        np.abs(image_distance_near - focus_distance)
        / image_distance_near
        * (focal_length**2 / (circle_of_confusion * (focus_distance - focal_length)))
    )
    fnum_far = (
        np.abs(image_distance_far - focus_distance)
        / image_distance_far
        * (focal_length**2 / (circle_of_confusion * (focus_distance - focal_length)))
    )

    fnum_near = min(max(fnum_near, 1), 32)
    fnum_far = min(max(fnum_far, 1), 32)

    return max(fnum_near, fnum_far, fnum_min)


def _find_lowest_exposure_time(camera: zivid.Camera) -> float:
    """Find the lowest exposure time [us] that a given camera can provide.

    Args:
        camera: Zivid camera

    Raises:
        RuntimeError: If camera model is not supported

    Returns:
        Lowest exposure time [us] for given camera

    """
    if camera.info.model in (
        zivid.CameraInfo.Model.zividTwo,
        zivid.CameraInfo.Model.zividTwoL100,
        zivid.CameraInfo.Model.zivid2PlusM130,
        zivid.CameraInfo.Model.zivid2PlusM60,
        zivid.CameraInfo.Model.zivid2PlusL110,
    ):
        exposure_time = 1677
    elif camera.info.model in (
        zivid.CameraInfo.Model.zivid2PlusMR130,
        zivid.CameraInfo.Model.zivid2PlusMR60,
        zivid.CameraInfo.Model.zivid2PlusLR110,
    ):
        exposure_time = 900
    else:
        raise RuntimeError("Unsupported camera model in this sample.")

    return exposure_time


def _find_max_brightness(camera: zivid.Camera) -> float:
    """Find the max projector brightness that a given camera can provide.

    Args:
        camera: Zivid camera

    Raises:
        RuntimeError: If camera model is not supported

    Returns:
        Highest projector brightness value for given camera

    """
    if camera.info.model in (zivid.CameraInfo.Model.zividTwo, zivid.CameraInfo.Model.zividTwoL100):
        brightness = 1.8
    elif camera.info.model in (
        zivid.CameraInfo.Model.zivid2PlusM130,
        zivid.CameraInfo.Model.zivid2PlusM60,
        zivid.CameraInfo.Model.zivid2PlusL110,
    ):
        brightness = 2.2
    elif camera.info.model in (
        zivid.CameraInfo.Model.zivid2PlusMR130,
        zivid.CameraInfo.Model.zivid2PlusMR60,
        zivid.CameraInfo.Model.zivid2PlusLR110,
    ):
        brightness = 2.5
    else:
        raise RuntimeError("Unsupported camera model in this sample.")

    return brightness


def _initialize_settings_2d(aperture: float, exposure_time: float, brightness: float, gain: float, pixel_sampling: str) -> zivid.Settings2D:
    """Initialize 2D capture settings.

    Args:
        aperture: Aperture
        exposure_time: Exposure time
        brightness: Projector brightness
        gain: Analog gain
        pixel_sampling: Pixel sampling

    Returns:
        Zivid 2D capture settings

    """

    settings_2d = zivid.Settings2D(
        acquisitions=[
            zivid.Settings2D.Acquisition(
                aperture=aperture,
                exposure_time=timedelta(microseconds=exposure_time),
                brightness=brightness,
                gain=gain,
            )
        ],
        processing=zivid.Settings2D.Processing(
            zivid.Settings2D.Processing.Color(gamma=1, balance=zivid.Settings2D.Processing.Color.Balance(1, 1, 1))
        ),
    )

    if pixel_sampling == "by2x2":
        settings_2d.sampling.pixel = zivid.Settings.Sampling.Pixel.by2x2
    elif pixel_sampling == "by4x4":
        settings_2d.sampling.pixel = zivid.Settings.Sampling.Pixel.by4x4
    else:
        print(f"Pixel sampling set to {pixel_sampling}, no resampling will be done.")

    return settings_2d


def _found_acquisition_settings_2d(max_mean_color: float, lower_limit: float, upper_limit: float) -> bool:
    """Check if the largest RGB value is within desired range.

    Args:
        max_mean_color: Highest of the averaged RGB channels in an RGB image
        lower_limit: Threshold for lowest acceptable value
        upper_limit: Threshold for highest acceptable value

    Returns:
        True if within limits, False otherwise

    """
    return lower_limit <= max_mean_color <= upper_limit


def _adjust_acquisition_settings_2d(
    settings_2d: zivid.Settings2D,
    adjustment_factor: float,
    tuning_index: int,
    min_fnum: float,
    min_exposure_time: float,
    max_gain_override: Optional[float] = None,
) -> int:
    """Adjust acquisition settings by an adjustment factor to update acquisition settings. Which setting to adjust is
    determined by the tuning index. The algorithm transitions through the following steps if the limit in each step is
    reached:
        Step 1: Change f-number (min: min_fnum, max: 32)
        Step 2: Change gain (min: 1, max: 2)
        Step 3: Change exposure time (min: min_exposure_time, max: 20000)
        Step 4: Change gain (min: 1, max: 4)
        Step 5: Change exposure time (min: min_exposure_time, max: 100000)
        Step 6: Change gain (min: 1, max: 16)

    Args:
        settings_2d: Zivid 2D settings
        adjustment_factor: Factor to adjust acquisition settings
        tuning_index: Current step in algorithm
        min_fnum: Lower f-number limit
        min_exposure_time: Lower exposure time limit for specific camera

    Returns:
        Updated tuning index

    """
    if tuning_index == 1:
        new_aperture = np.clip(settings_2d.acquisitions[0].aperture / adjustment_factor, min_fnum, 32)
        settings_2d.acquisitions[0].aperture = new_aperture
        print(f"Adjusted aperture: {new_aperture:.2f}")
        if new_aperture in (min_fnum, 32):
            tuning_index = 2

    elif tuning_index == 2:
        max_gain = max_gain_override or 2
        new_gain = np.clip(settings_2d.acquisitions[0].gain * adjustment_factor, 1, max_gain)
        settings_2d.acquisitions[0].gain = new_gain
        print(f"Adjusted gain: {new_gain:.2f}")
        if new_gain in (1, max_gain):
            tuning_index = 3

    elif tuning_index == 3:
        max_exposure_time = 20000
        new_exposure_time = timedelta(
            microseconds=np.clip(
                settings_2d.acquisitions[0].exposure_time.microseconds * adjustment_factor,
                min_exposure_time,
                max_exposure_time,
            )
        )
        settings_2d.acquisitions[0].exposure_time = new_exposure_time
        print(f"Adjusted exposure: {new_exposure_time.microseconds} [us]")
        if new_exposure_time in (
            timedelta(microseconds=min_exposure_time),
            timedelta(microseconds=max_exposure_time),
        ):
            tuning_index = 4

    elif tuning_index == 4:
        max_gain = max_gain_override or 4
        new_gain = np.clip(settings_2d.acquisitions[0].gain * adjustment_factor, 1, max_gain)
        settings_2d.acquisitions[0].gain = new_gain
        print(f"Adjusted gain to {new_gain:.2f}")
        if new_gain in (1, max_gain):
            tuning_index = 5

    elif tuning_index == 5:
        max_exposure_time = 100000
        new_exposure_time = timedelta(
            microseconds=np.clip(
                settings_2d.acquisitions[0].exposure_time.microseconds * adjustment_factor,
                min_exposure_time,
                max_exposure_time,
            )
        )
        settings_2d.acquisitions[0].exposure_time = new_exposure_time
        print(f"Adjusted exposure: {new_exposure_time.microseconds} [us]")
        if new_exposure_time in (
            timedelta(microseconds=min_exposure_time),
            timedelta(microseconds=max_exposure_time),
        ):
            tuning_index = 6

    elif tuning_index == 6:
        max_gain = max_gain_override or 16
        new_gain = np.clip(settings_2d.acquisitions[0].gain * adjustment_factor, 1, max_gain)
        settings_2d.acquisitions[0].gain = new_gain
        print(f"Adjusted gain to {new_gain:.2f}")
        if new_gain in (1, max_gain):
            tuning_index = 1

    return tuning_index


def _find_2d_settings_from_mask(
    camera: zivid.Camera,
    white_mask: np.ndarray,
    min_fnum: float,
    white_range: Tuple[float, float] = (210, 215),
    use_projector: bool = False,
    find_color_balance: bool = False,
    pixel_sampling: str = "none",
) -> zivid.Settings2D:
    """Find 2D settings automatically from the masked white reference area in a RGB image.

    Args:
        camera: Zivid camera
        white_mask: Mask of bools (H, W) for pixels containing the white object to calibrate against
        min_fnum: Lower limit on f-number for the calibrated settings
        use_projector: Use projector as part of acquisition settings
        find_color_balance: Set True to balance color, False otherwise
        pixel_sampling: Pixel sampling

    Raises:
        RuntimeError: If unable to find settings after sufficient number of tries

    Returns:
        settings_2d: Zivid 2D settings

    """
    min_exposure_time = _find_lowest_exposure_time(camera)
    print(f"Lowest exposure time: {min_exposure_time} [us]")

    projector_brightness = _find_max_brightness(camera) if use_projector else 0
    print(f"Max projector brightness: {projector_brightness}")

    settings_2d = _initialize_settings_2d(aperture=8, exposure_time=min_exposure_time, brightness=projector_brightness, gain=1, pixel_sampling=pixel_sampling)
    print(f"Initial settings 2D: {settings_2d}")

    tuning_index = 1
    count = 0
    while True:
        print(f"Iteration {count + 1}, tuning index: {tuning_index}")
        rgb = _capture_rgb(camera, settings_2d)
        mean_rgb = compute_mean_rgb_from_mask(rgb, white_mask)
        max_mean_color = mean_rgb.max()

        found_acquisition_settings = _found_acquisition_settings_2d(
            max_mean_color, white_range[0], white_range[1]
        )

        if found_acquisition_settings:
            break

        acquisition_factor = float(np.mean(list(white_range)) / max_mean_color)
        tuning_index = _adjust_acquisition_settings_2d(
            settings_2d, acquisition_factor, tuning_index, min_fnum, min_exposure_time
        )

        count = count + 1
        if count > 20:
            raise RuntimeError("Unable to find settings in current lighting")

    if find_color_balance:
        if camera_may_need_color_balancing(camera):
            red_balance, green_balance, blue_balance = white_balance_calibration(camera, settings_2d, white_mask)

            settings_2d.processing.color.balance.red = red_balance
            settings_2d.processing.color.balance.green = green_balance
            settings_2d.processing.color.balance.blue = blue_balance
        else:
            print(f"{camera.info.model_name} does not need color balancing, skipping ...")

    return settings_2d


def _print_poor_pixel_distribution(rgb: np.ndarray) -> None:
    """Print distribution of bad pixels (saturated or completely black) in an RGB image.

    Args:
        rgb: RGB image (H, W, 3)

    """
    total_num_pixels = rgb.shape[0] * rgb.shape[1]

    saturated_or = np.sum(np.logical_or(np.logical_or(rgb[:, :, 0] == 255, rgb[:, :, 1] == 255), rgb[:, :, 2] == 255))
    saturated_and = np.sum(
        np.logical_and(np.logical_and(rgb[:, :, 0] == 255, rgb[:, :, 1] == 255), rgb[:, :, 2] == 255)
    )

    black_or = np.sum(np.logical_or(np.logical_or(rgb[:, :, 0] == 0, rgb[:, :, 1] == 0), rgb[:, :, 2] == 0))
    black_and = np.sum(np.logical_and(np.logical_and(rgb[:, :, 0] == 0, rgb[:, :, 1] == 0), rgb[:, :, 2] == 0))

    print("Distribution of saturated (255) and black (0) pixels with final settings:")
    print(f"Saturated pixels (at least one channel): {saturated_or}\t ({100 * saturated_or / total_num_pixels:.2f}%)")
    print(f"Saturated pixels (all channels):\t {saturated_and}\t ({100 * saturated_and / total_num_pixels:.2f}%)")
    print(f"Black pixels (at least one channel):\t {black_or}\t ({100 * black_or / total_num_pixels:.2f}%)")
    print(f"Black pixels (all channels):\t\t {black_and}\t ({100 * black_and / total_num_pixels:.2f}%)")


def _plot_image_with_histogram(rgb: np.ndarray, settings_2d: zivid.Settings2D, out_path: Path) -> None:
    """Show an RGB image with its histogram (grayscale) in linear scale and save it to a file.

    Args:
        rgb: RGB image (H, W, 3)
        settings_2d: Zivid 2D settings

    """
    grayscale = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    grayscale_raveled = grayscale.ravel()

    fig, axs = plt.subplots(2, 1, figsize=(8, 8), gridspec_kw={"height_ratios": [1, 3]})

    exposure_time = settings_2d.acquisitions[0].exposure_time.microseconds
    aperture = settings_2d.acquisitions[0].aperture
    brightness = settings_2d.acquisitions[0].brightness
    gain = settings_2d.acquisitions[0].gain

    red = settings_2d.processing.color.balance.red
    green = settings_2d.processing.color.balance.green
    blue = settings_2d.processing.color.balance.blue

    fig.suptitle(
        f"Histogram and image with settings:\n"
        f"ET: {exposure_time}, A: {aperture:.2f}, B: {brightness}, G: {gain:.2f}\n\n"
        f"Color balance (R, G, B): {red:.2f}, {green:.2f}, {blue:.2f}"
    )

    axs[0].hist(grayscale_raveled, bins=np.arange(0, 256), color="gray")
    axs[0].yaxis.set_visible(False)

    axs[1].imshow(rgb)
    axs[1].xaxis.set_visible(False)
    axs[1].yaxis.set_visible(False)

    fig.savefig(str(out_path), bbox_inches="tight", dpi=300)

def _log_image(img: np.ndarray, out_path: Path) -> None:
    if img.size == 0:
        print("Skipping saving of empty image")
        return

    if out_path.exists():
        print(f"Overwriting existing file: {out_path}")

    cv2.imwrite(str(out_path), img)
    print(f"Image saved to {out_path}")

def _main() -> None:
    app = zivid.Application()

    user_options = _options()

    if not user_options.calibration_id:
        user_options.calibration_id = str(_get_current_time_ms())

    # Setup log dir
    log_dir = user_options.log_dir / user_options.calibration_id
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"Log directory: {log_dir}")

    print("Connecting to camera")
    camera = app.connect_camera()
    print(f"Connected to camera: {str(camera)}")

    # Find checkerboard in full resolution, then resize if required
    print("Finding the white squares of the checkerboard as white reference ...")
    rgb_full_res, white_mask_full_res, checkerboard_distance = _find_white_mask_and_distance_to_checkerboard(camera)
    print(f"Initial RGB image shape (checkerboard): {rgb_full_res.shape}")
    print(f"Initial white squres mask shape (checkerboard): {white_mask_full_res.shape}")
    _log_image(rgb_full_res, log_dir / "checkerboard_rgb.png")
    _log_image(white_mask_full_res.astype(np.uint8) * 255, log_dir / "checkerboard_white_mask.png")

    # Resize mask to match pixel sampling mode
    if user_options.pixel_sampling == "by2x2":
        resize_factor = 0.5
    elif user_options.pixel_sampling == "by4x4":
        resize_factor = 0.25
    else:
        resize_factor = 1

    white_mask = cv2.resize(white_mask_full_res, None, fx=resize_factor, fy=resize_factor, interpolation=cv2.INTER_NEAREST)
    _log_image(white_mask.astype(np.uint8) * 255, log_dir / "checkerboard_white_mask_resized.png")

    # Determine lowest acceptable f-number to be in focus
    if user_options.checkerboard_at_start_of_range:
        image_distance_near = checkerboard_distance
        image_distance_far = image_distance_near + user_options.desired_focus_range
    else:
        image_distance_far = checkerboard_distance
        image_distance_near = image_distance_far - user_options.desired_focus_range
    print(f"Computed distance range: [{image_distance_near:.2f}, {image_distance_far:.2f}] [mm]")

    min_fnum = _find_lowest_acceptable_fnum(camera, image_distance_near, image_distance_far, max_gain_override=user_options.max_gain_override)
    print(f"Lowest acceptable f-number: {min_fnum:.2f}")

    print("Finding 2D settings via white mask ...")
    settings_2d = _find_2d_settings_from_mask(
        camera,
        white_mask,
        min_fnum,
        white_range=user_options.desired_white_range,
        use_projector=user_options.use_projector,
        find_color_balance=user_options.find_color_balance,
        pixel_sampling=user_options.pixel_sampling
    )

    print(f"Automatic 2D settings: {str(settings_2d)}")
    out_settings_path = log_dir / "Automatic2DSettings.yml"
    settings_2d.save(out_settings_path)
    print(f"Saved settings to: {out_settings_path}")

    # Capture RGB image with the found settings for visualization
    rgb = _capture_rgb(camera, settings_2d)
    _log_image(rgb, log_dir / "post_calibration_rgb.png")

    _print_poor_pixel_distribution(rgb)
    _plot_image_with_histogram(rgb, settings_2d, out_path=log_dir / "post_calibration_histogram.png")


if __name__ == "__main__":
    _main()
