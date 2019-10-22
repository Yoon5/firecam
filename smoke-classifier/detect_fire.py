# Copyright 2018 The Fuego Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""
@author: Kinshuk Govil

This is the main code for reading images from webcams and detecting fires

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
fuegoRoot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(fuegoRoot, 'lib'))
sys.path.insert(0, fuegoRoot)
import settings
settings.fuegoRoot = fuegoRoot
import collect_args
import rect_to_squares
import goog_helper
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' # quiet down tensorflow logging (must be done before tf_helper)
import tf_helper
import db_manager
import email_helper
import sms_helper
import img_archive
from gcp_helper import connect_to_prediction_service, predict_batch


import logging
import pathlib
import tempfile
import shutil
import time, datetime, dateutil.parser
import random
import re
import hashlib
from urllib.request import urlretrieve
import numpy as np
from PIL import Image, ImageFile, ImageDraw, ImageFont
ImageFile.LOAD_TRUNCATED_IMAGES = True


def getNextImage(dbManager, cameras, cameraID=None):
    """Gets the next image to check for smoke

    Uses a shared counter being updated by all cooperating detection processes
    to index into the list of cameras to download the image to a local
    temporary directory

    Args:
        dbManager (DbManager):
        cameras (list): list of cameras
        cameraID (str): optional specific camera to get image from

    Returns:
        Tuple containing camera name, current timestamp, and filepath of the image
    """
    if getNextImage.tmpDir == None:
        getNextImage.tmpDir = tempfile.TemporaryDirectory()
        logging.warning('TempDir %s', getNextImage.tmpDir.name)

    if cameraID:
        camera = list(filter(lambda x: x['name'] == cameraID, cameras))[0]
    else:
        index = dbManager.getNextSourcesCounter() % len(cameras)
        camera = cameras[index]
    timestamp = int(time.time())
    imgPath = img_archive.getImgPath(getNextImage.tmpDir.name, camera['name'], timestamp)
    # logging.warning('urlr %s %s', camera['url'], imgPath)
    try:
        urlretrieve(camera['url'], imgPath)
    except Exception as e:
        logging.error('Error fetching image from %s %s', camera['name'], str(e))
        return getNextImage(dbManager, cameras)
    md5 = hashlib.md5(open(imgPath, 'rb').read()).hexdigest()
    if ('md5' in camera) and (camera['md5'] == md5) and not cameraID:
        logging.warning('Camera %s image unchanged', camera['name'])
        # skip to next camera
        return getNextImage(dbManager, cameras)
    camera['md5'] = md5
    return (camera['name'], timestamp, imgPath, md5)
getNextImage.tmpDir = None

# XXXXX Use a fixed stable directory for testing
# from collections import namedtuple
# Tdir = namedtuple('Tdir', ['name'])
# getNextImage.tmpDir = Tdir('c:/tmp/dftest')


def getNextImageFromDir(imgDirectory):
    """Gets the next image to check for smoke from given directory

    A variant of getNextImage() above but works with files already present
    on the locla filesystem.

    Args:
        imgDirectory (str): directory containing the files

    Returns:
        Tuple containing camera name, current timestamp, and filepath of the image
    """
    if getNextImageFromDir.tmpDir == None:
        getNextImageFromDir.tmpDir = tempfile.TemporaryDirectory()
        logging.warning('TempDir %s', getNextImageFromDir.tmpDir.name)
    if not getNextImageFromDir.files:
        allFiles = os.listdir(imgDirectory)
        # filter out files with _Score suffix because they contain annotated scores
        # generated by drawFireBox() function below.
        getNextImageFromDir.files = list(filter(lambda x: '_Score.jpg' not in x, allFiles))
    getNextImageFromDir.index += 1
    if getNextImageFromDir.index < len(getNextImageFromDir.files):
        fileName = getNextImageFromDir.files[getNextImageFromDir.index]
        origPath = os.path.join(imgDirectory, fileName)
        destPath = os.path.join(getNextImageFromDir.tmpDir.name, fileName)
        shutil.copyfile(origPath, destPath)
        parsed = img_archive.parseFilename(fileName)
        if not parsed:
            # failed to parse, so skip to next image
            return getNextImageFromDir(imgDirectory)
        md5 = hashlib.md5(open(destPath, 'rb').read()).hexdigest()
        return (parsed['cameraID'], parsed['unixTime'], destPath, md5)
    logging.warning('Finished processing all images in directory. Exiting')
    exit(0)
getNextImageFromDir.files = None
getNextImageFromDir.index = -1
getNextImageFromDir.tmpDir = None


def segmentImage(imgPath):
    """Segment the given image into sections to for smoke classificaiton

    Args:
        imgPath (str): filepath of the image

    Returns:
        List of dictionary containing information on each segment
    """
    img = Image.open(imgPath)
    image_crops, segment_infos = rect_to_squares.cutBoxes(img)
    img.close()
    return image_crops, segment_infos


def recordScores(dbManager, camera, timestamp, segments, minusMinutes):
    """Record the smoke scores for each segment into SQL DB

    Args:
        dbManager (DbManager):
        camera (str): camera name
        timestamp (int):
        segments (list): List of dictionary containing information on each segment
    """
    dt = datetime.datetime.fromtimestamp(timestamp)
    secondsInDay = (dt.hour * 60 + dt.minute) * 60 + dt.second

    dbRows = []
    for segmentInfo in segments:
        dbRow = {
            'CameraName': camera,
            'Timestamp': timestamp,
            'MinX': segmentInfo['MinX'],
            'MinY': segmentInfo['MinY'],
            'MaxX': segmentInfo['MaxX'],
            'MaxY': segmentInfo['MaxY'],
            'Score': segmentInfo['score'],
            'MinusMinutes': minusMinutes,
            'SecondsInDay': secondsInDay
        }
        dbRows.append(dbRow)
    dbManager.add_data('scores', dbRows)


def postFilter(dbManager, camera, timestamp, segments):
    """Post classification filter to reduce false positives

    Many times smoke classification scores segments with haze and glare
    above 0.5.  Haze and glare occur tend to occur at similar time over
    multiple days, so this filter raises the threshold based on the max
    smoke score for same segment at same time of day over the last few days.
    Score must be > halfway between max value and 1.  Also, minimum .1 above max.

    Args:
        dbManager (DbManager):
        camera (str): camera name
        timestamp (int):
        segments (list): Sorted List of dictionary containing information on each segment

    Returns:
        Dictionary with information for the segment most likely to be smoke
        or None
    """
    # enable the next few lines fakes a detection to test alerting functionality
    # maxFireSegment = segments[0]
    # maxFireSegment['HistAvg'] = 0.1
    # maxFireSegment['HistMax'] = 0.2
    # maxFireSegment['HistNumSamples'] = 10
    # return maxFireSegment

    # segments is sorted, so skip all work if max score is < .5
    if segments[0]['score'] < .5:
        return None

    sqlTemplate = """SELECT MinX,MinY,MaxX,MaxY,count(*) as cnt, avg(score) as avgs, max(score) as maxs FROM scores
    WHERE CameraName='%s' and Timestamp > %s and Timestamp < %s and SecondsInDay > %s and SecondsInDay < %s
    GROUP BY MinX,MinY,MaxX,MaxY"""

    dt = datetime.datetime.fromtimestamp(timestamp)
    secondsInDay = (dt.hour * 60 + dt.minute) * 60 + dt.second
    sqlStr = sqlTemplate % (camera, timestamp - 60*60*int(24*3.5), timestamp - 60*60*12, secondsInDay - 60*60, secondsInDay + 60*60)
    # print('sql', sqlStr, timestamp)
    dbResult = dbManager.query(sqlStr)
    # if len(dbResult) > 0:
    #     print('post filter result', dbResult)
    maxFireSegment = None
    maxFireScore = 0
    for segmentInfo in segments:
        if segmentInfo['score'] < .5: # segments is sorted. we've reached end of segments >= .5
            break
        for row in dbResult:
            if (row['minx'] == segmentInfo['MinX'] and row['miny'] == segmentInfo['MinY'] and
                row['maxx'] == segmentInfo['MaxX'] and row['maxy'] == segmentInfo['MaxY']):
                threshold = (row['maxs'] + 1)/2 # threshold is halfway between max and 1
                # Segments with historical value above 0.8 are too noisy, so discard them by setting
                # threshold at least .2 above max.  Also requires .7 to reach .9 vs just .85
                threshold = max(threshold, row['maxs'] + 0.2)
                # print('thresh', row['minx'], row['miny'], row['maxx'], row['maxy'], row['maxs'], threshold)
                if (segmentInfo['score'] > threshold) and (segmentInfo['score'] > maxFireScore):
                    maxFireScore = segmentInfo['score']
                    maxFireSegment = segmentInfo
                    maxFireSegment['HistAvg'] = row['avgs']
                    maxFireSegment['HistMax'] = row['maxs']
                    maxFireSegment['HistNumSamples'] = row['cnt']

    return maxFireSegment


def collectPositves(service, imgPath, origImgPath, segments):
    """Collect all positive scoring segments

    Copy the images for all segments that score highter than > .5 to google drive folder
    settings.positivePictures. These will be used to train future models.
    Also, copy the full image for reference.

    Args:
        imgPath (str): path name for main image
        segments (list): List of dictionary containing information on each segment
    """
    positiveSegments = 0
    ppath = pathlib.PurePath(origImgPath)
    imgNameNoExt = str(os.path.splitext(ppath.name)[0])
    origImg = None
    for segmentInfo in segments:
        if segmentInfo['score'] > .5:
            if imgPath != origImgPath:

                if not origImg:
                    origImg = Image.open(origImgPath)
                cropCoords = (segmentInfo['MinX'], segmentInfo['MinY'], segmentInfo['MaxX'], segmentInfo['MaxY'])
                croppedOrigImg = origImg.crop(cropCoords)
                cropImgName = imgNameNoExt + '_Crop_' + 'x'.join(list(map(lambda x: str(x), cropCoords))) + '.jpg'
                cropImgPath = os.path.join(str(ppath.parent), cropImgName)
                croppedOrigImg.save(cropImgPath, format='JPEG')
                croppedOrigImg.close()
                if hasattr(settings, 'positivePicturesDir'):
                    destPath = os.path.join(settings.positivePicturesDir, cropImgName)
                    shutil.copy(cropImgPath, destPath)
                else:
                    goog_helper.uploadFile(service, settings.positivePictures, cropImgPath)
                os.remove(cropImgPath)

            #do cropping now that we want to add to training set
            imgName = pathlib.PurePath(origImgPath).name
            outputDir = str(pathlib.PurePath(origImgPath).parent)
            imgNameNoExt = str(os.path.splitext(imgName)[0])
            coords = (segmentInfo['MinX'], segmentInfo['MinY'], segmentInfo['MaxX'], segmentInfo['MaxY'])
            # output cropped image
            cropImgName = imgNameNoExt + '_Crop_' + 'x'.join(list(map(lambda x: str(x), coords))) + '.jpg'
            cropImgPath = os.path.join(outputDir, cropImgName)
            origImg = Image.open(origImgPath)
            cropped_img = origImg.crop(coords)
            cropped_img.save(cropImgPath, format='JPEG')
            cropped_img.close()

            if hasattr(settings, 'positivePicturesDir'):
                pp = pathlib.PurePath(cropImgPath)
                destPath = os.path.join(settings.positivePicturesDir, pp.name)
                shutil.copy(cropImgPath, destPath)
            else:
                goog_helper.uploadFile(service, settings.positivePictures, cropImgPath)
            #delete the cropped file
            os.remove(cropImgPath)
            positiveSegments += 1

    if positiveSegments > 0:
        # Commenting out saving full images for now to reduce data
        # goog_helper.uploadFile(service, settings.positivePictures, imgPath)
        logging.warning('Found %d positives in image %s', positiveSegments, ppath.name)


def drawRect(imgDraw, x0, y0, x1, y1, width, color):
    for i in range(width):
        imgDraw.rectangle((x0+i,y0+i,x1-i,y1-i),outline=color)


def drawFireBox(imgPath, fireSegment):
    """Draw bounding box with fire detection with score on image

    Stores the resulting annotated image as new file

    Args:
        imgPath (str): filepath of the image

    Returns:
        filepath of new image file
    """
    img = Image.open(imgPath)
    imgDraw = ImageDraw.Draw(img)
    x0 = fireSegment['MinX']
    y0 = fireSegment['MinY']
    x1 = fireSegment['MaxX']
    y1 = fireSegment['MaxY']
    centerX = (x0 + x1)/2
    centerY = (y0 + y1)/2
    color = "red"
    lineWidth=3
    drawRect(imgDraw, x0, y0, x1, y1, lineWidth, color)

    fontSize=80
    font = ImageFont.truetype(os.path.join(settings.fuegoRoot, 'lib/Roboto-Regular.ttf'), size=fontSize)
    scoreStr = '%.2f' % fireSegment['score']
    textSize = imgDraw.textsize(scoreStr, font=font)
    imgDraw.text((centerX - textSize[0]/2, centerY - textSize[1]), scoreStr, font=font, fill=color)

    color = "blue"
    fontSize=70
    font = ImageFont.truetype(os.path.join(settings.fuegoRoot, 'lib/Roboto-Regular.ttf'), size=fontSize)
    scoreStr = '%.2f' % fireSegment['HistMax']
    textSize = imgDraw.textsize(scoreStr, font=font)
    imgDraw.text((centerX - textSize[0]/2, centerY), scoreStr, font=font, fill=color)

    filePathParts = os.path.splitext(imgPath)
    annotatedFile = filePathParts[0] + '_Score' + filePathParts[1]
    img.save(annotatedFile, format="JPEG")
    del imgDraw
    img.close()
    return annotatedFile


def recordDetection(dbManager, service, camera, timestamp, imgPath, annotatedFile, fireSegment):
    """Record that a smoke/fire has been detected

    Record the detection with useful metrics in 'detections' table in SQL DB.
    Also, upload image file to google drive

    Args:
        dbManager (DbManager):
        service:
        camera (str): camera name
        timestamp (int):
        imgPath: filepath of the image
        annotatedFile: filepath of the image with annotated box and score
        fireSegment (dictionary): dictionary with information for the segment with fire/smoke

    Returns:
        List of Google drive IDs for the uploaded image files
    """
    logging.warning('Fire detected by camera %s, image %s, segment %s', camera, imgPath, str(fireSegment))
    # upload file to google drive detection dir
    driveFileIDs = []
    driveFile = goog_helper.uploadFile(service, settings.detectionPictures, imgPath)
    if driveFile:
        driveFileIDs.append(driveFile['id'])
    driveFile = goog_helper.uploadFile(service, settings.detectionPictures, annotatedFile)
    if driveFile:
        driveFileIDs.append(driveFile['id'])
    logging.warning('Uploaded to google drive detections folder %s', str(driveFileIDs))

    dbRow = {
        'CameraName': camera,
        'Timestamp': timestamp,
        'MinX': fireSegment['MinX'],
        'MinY': fireSegment['MinY'],
        'MaxX': fireSegment['MaxX'],
        'MaxY': fireSegment['MaxY'],
        'Score': fireSegment['score'],
        'HistAvg': fireSegment['HistAvg'],
        'HistMax': fireSegment['HistMax'],
        'HistNumSamples': fireSegment['HistNumSamples'],
        'ImageID': driveFileIDs[0] if driveFileIDs else ''
    }
    dbManager.add_data('detections', dbRow)
    return driveFileIDs


def checkAndUpdateAlerts(dbManager, camera, timestamp, driveFileIDs):
    """Check if alert has been recently sent out for given camera

    If an alert of this camera has't been recorded recently, record this as an alert

    Args:
        dbManager (DbManager):
        camera (str): camera name
        timestamp (int):
        driveFileIDs (list): List of Google drive IDs for the uploaded image files

    Returns:
        True if this is a new alert, False otherwise
    """
    sqlTemplate = """SELECT * FROM alerts
    where CameraName='%s' and timestamp > %s"""
    sqlStr = sqlTemplate % (camera, timestamp - 60*60*2) # suppress alerts for 2 hours
    dbResult = dbManager.query(sqlStr)
    if len(dbResult) > 0:
        logging.warning('Supressing new alert due to recent alert')
        return False

    dbRow = {
        'CameraName': camera,
        'Timestamp': timestamp,
        'ImageID': driveFileIDs[0] if driveFileIDs else ''
    }
    dbManager.add_data('alerts', dbRow)
    return True


def alertFire(constants, cameraID, imgPath, annotatedFile, driveFileIDs, fireSegment, timestamp):
    """Send alerts about given fire through all channels (currently email and sms)

    Args:
        constants (dict): "global" contants
        cameraID (str): camera name
        imgPath: filepath of the original image
        annotatedFile: filepath of the annotated image
        driveFileIDs (list): List of Google drive IDs for the uploaded image files
        fireSegment (dictionary): dictionary with information for the segment with fire/smoke
        timestamp (int): time.time() value when image was taken
    """
    emailFireNotification(constants, cameraID, imgPath, annotatedFile, driveFileIDs, fireSegment, timestamp)
    smsFireNotification(constants['dbManager'], cameraID)


def emailFireNotification(constants, cameraID, imgPath, annotatedFile, driveFileIDs, fireSegment, timestamp):
    """Send an email alert for a potential new fire

    Send email with information about the camera and fire score includeing
    image attachments

    Args:
        constants (dict): "global" contants
        cameraID (str): camera name
        imgPath: filepath of the original image
        annotatedFile: filepath of the annotated image
        driveFileIDs (list): List of Google drive IDs for the uploaded image files
        fireSegment (dictionary): dictionary with information for the segment with fire/smoke
        timestamp (int): time.time() value when image was taken
    """
    dbManager = constants['dbManager']
    subject = 'Possible (%d%%) fire in camera %s' % (int(fireSegment['score']*100), cameraID)
    body = 'Please check the attached images for fire.'
    # commenting out links to google drive because they appear as extra attachments causing confusion
    # and some email recipients don't even have permissions to access drive.
    # for driveFileID in driveFileIDs:
    #     driveTempl = '\nAlso available from google drive as https://drive.google.com/file/d/%s'
    #     driveBody = driveTempl % driveFileID
    #     body += driveBody

    # emails are sent from settings.fuegoEmail and bcc to everyone with active emails in notifications SQL table
    dbResult = dbManager.getNotifications(filterActiveEmail = True)
    emails = [x['email'] for x in dbResult]
    if len(emails) > 0:
        # attach images spanning a few minutes so reviewers can evaluate based on progression
        startTimeDT = datetime.datetime.fromtimestamp(timestamp - 3*60)
        endTimeDT = datetime.datetime.fromtimestamp(timestamp - 1*60)
        with tempfile.TemporaryDirectory() as tmpDirName:
            oldImages = img_archive.getHpwrenImages(constants['googleServices'], settings, tmpDirName,
                                                    constants['camArchives'], cameraID, startTimeDT, endTimeDT, 1)
            oldImages = oldImages or []
            attachments = oldImages + [imgPath, annotatedFile]
            email_helper.sendEmail(constants['googleServices']['mail'], settings.fuegoEmail, emails, subject, body, attachments)


def smsFireNotification(dbManager, cameraID):
    """Send an sms (phone text message) alert for a potential new fire

    Args:
        dbManager (DbManager):
        cameraID (str): camera name
    """
    message = 'Fuego fire notification in camera %s. Please check email for details' % cameraID
    dbResult = dbManager.getNotifications(filterActivePhone = True)
    phones = [x['phone'] for x in dbResult]
    if len(phones) > 0:
        for phone in phones:
            sms_helper.sendSms(settings, phone, message)


def deleteImageFiles(imgPath, origImgPath, annotatedFile):
    """Delete all image files given in segments

    Args:
        imgPath: filepath of the original image
        annotatedFile: filepath of the annotated image
        segments (list): List of dictionary containing information on each segment
    """
    os.remove(imgPath)
    if imgPath != origImgPath:
        os.remove(origImgPath)
    if annotatedFile:
        os.remove(annotatedFile)
    # leftoverFiles = os.listdir(str(ppath.parent))
    # if len(leftoverFiles) > 0:
    #     logging.warning('leftover files %s', str(leftoverFiles))


def getLastScoreCamera(dbManager):
    sqlStr = "SELECT CameraName from scores order by Timestamp desc limit 1;"
    dbResult = dbManager.query(sqlStr)
    if len(dbResult) > 0:
        return dbResult[0]['CameraName']
    return None


def heartBeat(filename):
    """Inform monitor process that this detection process is alive

    Informs by updating the timestamp on given file

    Args:
        filename (str): file path of file used for heartbeating
    """
    pathlib.Path(filename).touch()


def classify(image_crops, segment_infos, prediction_service):
    """Classify each square

    Args:
        imgPath (str): filepath of the image to segment and clasify
        tfSession: Tensorflow session
        graph: Tensorflow graph
        labels: Tensorflow labels

    Returns:
        list of segments with scores sorted by decreasing score
    """
    batch = np.stack(image_crops)
    predictions = predict_batch(prediction_service, batch)

    #put predictions into expected form
    for idx, segmentInfo in enumerate(segment_infos):
        segmentInfo['score'] = float(predictions[idx, 1])

    segment_infos.sort(key=lambda x: -x['score'])
    return segment_infos


def recordFilterReport(constants, cameraID, timestamp, imgPath, origImgPath, segments, minusMinutes, googleDrive, positivesOnly):
    """Record the scores for classified segments, check for detections and alerts, and delete images

    Args:
        constants (dict): "global" contants
        cameraID (str): ID for camera associated with the image
        timestamp (int): time.time() value when image was taken
        imgPath (str): filepath of the image (possibly derived image by subtraction) that was classified
        origImgPath (str): filepath of the original image from camera
        segments (list): List of dictionary containing information on each segment
        minusMinutes (int): number of minutes separating subtracted images (0 for non-subtracted images)
        googleDrive: google drive API service
        positivesOnly (bool): only collect/upload positives to google drive - used when training against old images
    """
    annotatedFile = None
    args = constants['args']
    dbManager = constants['dbManager']
    if args.collectPositves:
        collectPositves(googleDrive, imgPath, origImgPath, segments)
    if not positivesOnly:
        recordScores(dbManager, cameraID, timestamp, segments, minusMinutes)
        fireSegment = postFilter(dbManager, cameraID, timestamp, segments)
        if fireSegment:
            annotatedFile = drawFireBox(origImgPath, fireSegment)
            driveFileIDs = recordDetection(dbManager, googleDrive, cameraID, timestamp, origImgPath, annotatedFile, fireSegment)
            if checkAndUpdateAlerts(dbManager, cameraID, timestamp, driveFileIDs):
                alertFire(constants, cameraID, origImgPath, annotatedFile, driveFileIDs, fireSegment, timestamp)
    deleteImageFiles(imgPath, origImgPath, annotatedFile)
    if (args.heartbeat):
        heartBeat(args.heartbeat)
    logging.warning('Highest score for camera %s: %f' % (cameraID, segments[0]['score']))


def genDiffImage(imgPath, earlierImgPath, minusMinutes):
    """Subtract the two given images and store result in new difference image file

    Args:
        imgPath (str): filepath of the current image (to subtract from)
        imgPath (str): filepath of the earlier image (value to subtract)
        minusMinutes (int): number of minutes separating subtracted images

    Returns:
        file path to the difference image
    """
    imgA = Image.open(imgPath)
    imgB = Image.open(earlierImgPath)
    imgDiff = img_archive.diffImages(imgA, imgB)
    parsedName = img_archive.parseFilename(imgPath)
    parsedName['diffMinutes'] = minusMinutes
    imgDiffName = img_archive.repackFileName(parsedName)
    ppath = pathlib.PurePath(imgPath)
    imgDiffPath = os.path.join(str(ppath.parent), imgDiffName)
    imgDiff.save(imgDiffPath, format='JPEG')
    return imgDiffPath


def expectedDrainSeconds(deferredImages, timeTracker):
    """Calculate the expected time to drain the deferred images queue

    Args:
        deferredImages (list): list of deferred images
        timeTracker (dict): tracks recent image processing times

    Returns:
        Seconds to process deferred images
    """
    return len(deferredImages)*timeTracker['timePerSample']


def updateTimeTracker(timeTracker, processingTime):
    """Update the time tracker data with given time to process current image

    If enough samples new samples have been reorded, resets the history and
    updates the average timePerSample

    Args:
        timeTracker (dict): tracks recent image processing times
        processingTime (float): number of seconds needed to process current image
    """
    timeTracker['totalTime'] += processingTime
    timeTracker['numSamples'] += 1
    # after N samples, update the rate to adapt to current conditions
    # N = 50 should be big enough to be stable yet small enough to adapt
    if timeTracker['numSamples'] > 50:
        timeTracker['timePerSample'] = timeTracker['totalTime'] / timeTracker['numSamples']
        timeTracker['totalTime'] = 0
        timeTracker['numSamples'] = 0
        logging.warning('New timePerSample %.2f', timeTracker['timePerSample'])


def initializeTimeTracker():
    """Initialize the time tracker

    Returns:
        timeTracker (dict):
    """
    return {
        'totalTime': 0.0,
        'numSamples': 0,
        'timePerSample': 3 # start off with estimate of 3 seconds per camera
    }


def getDeferrredImgageInfo(deferredImages, timeTracker, minusMinutes, currentTime):
    """Returns tuple of boolean indicating whether queue is full, and optionally deferred image if ready to process

    Args:
        deferredImages (list): list of deferred images
        timeTracker (dict): tracks recent image processing times
        minusMinutes (int): number of desired minutes between images to subract
        currentTime (float): current time.time() value

    Returns:
        Tuple (bool, dict or None)
    """
    if minusMinutes == 0:
        return (False, None)
    if len(deferredImages) == 0:
        return (False, None)
    minusSeconds = 60*minusMinutes
    queueFull = (expectedDrainSeconds(deferredImages, timeTracker) >= minusSeconds)
    if queueFull or (deferredImages[0]['timestamp'] + minusSeconds < currentTime):
        img = deferredImages[0]
        del deferredImages[0]
        return (queueFull, img)
    return (queueFull, None)


def addToDeferredImages(dbManager, cameras, deferredImages):
    """Fetch a new image and add it to deferred images queue

    Args:
        dbManager (DbManager):
        cameras (list): list of cameras
        deferredImages (list): list of deferred images
    """
    (cameraID, timestamp, imgPath, md5) = getNextImage(dbManager, cameras)
    # add image to Q if not already another one from same camera
    matches = list(filter(lambda x: x['cameraID'] == cameraID, deferredImages))
    if len(matches) > 0:
        assert len(matches) == 1
        logging.warning('Camera already in list waiting processing %s', matches[0])
        time.sleep(2) # take a nap to let things catch up
        return
    deferredImages.append({
        'timestamp': timestamp,
        'cameraID': cameraID,
        'imgPath': imgPath,
        'md5': md5,
        'oldWait': 0
    })
    logging.warning('Defer camera %s.  Len %d', cameraID, len(deferredImages))


def genDiffImageFromDeferred(dbManager, cameras, deferredImageInfo, deferredImages, minusMinutes):
    """Generate a difference image baed on given deferred image

    Args:
        dbManager (DbManager):
        cameras (list): list of cameras
        deferredImageInfo (dict): deferred image to process
        deferredImages (list): list of deferred images
        minusMinutes (int): number of desired minutes between images to subract

    Returns:
        Tuple containing camera name, current timestamp, filepath of regular image, and filepath of difference image
    """
    # logging.warning('DefImg: %d, %s, %s', len(deferredImages), timeStart, deferredImageInfo)
    (cameraID, timestamp, imgPath, md5) = getNextImage(dbManager, cameras, deferredImageInfo['cameraID'])
    timeDiff = timestamp - deferredImageInfo['timestamp']
    if md5 == deferredImageInfo['md5']: # check if image hasn't changed
        logging.warning('Camera %s unchanged (oldWait=%d, diff=%d)', cameraID, deferredImageInfo['oldWait'], timeDiff)
        if (timeDiff + deferredImageInfo['oldWait']) < min(2 * 60 * minusMinutes, 5 * 60):
            # some cameras may not referesh fast enough so give another chance up to 2x minusMinutes or 5 mins
            # Putting it back at the end of the queue with updated timestamp
            # Updating timestamp so this doesn't take priority in processing
            deferredImageInfo['timestamp'] = timestamp
            deferredImageInfo['oldWait'] += timeDiff
            deferredImages.append(deferredImageInfo)
        else: # timeout waiting for new image from camera => discard data
            os.remove(deferredImageInfo['imgPath'])
        os.remove(imgPath)
        return (None,None,None,None)
    imgDiffPath = genDiffImage(imgPath, deferredImageInfo['imgPath'], minusMinutes)
    logging.warning('Checking camera %s.  Len %d. Diff %d', cameraID, len(deferredImages), timeDiff)
    return (cameraID, timestamp, imgPath, imgDiffPath)


def getArchivedImages(constants, cameras, startTimeDT, timeRangeSeconds, minusMinutes):
    """Get random images from HPWREN archive matching given constraints and optionally subtract them

    Args:
        constants (dict): "global" contants
        cameras (list): list of cameras
        startTimeDT (datetime): starting time of time range
        timeRangeSeconds (int): number of seconds in time range
        minusMinutes (int): number of desired minutes between images to subract

    Returns:
        Tuple containing camera name, current timestamp, filepath of regular image, and filepath of difference image
    """
    if getArchivedImages.tmpDir == None:
        getArchivedImages.tmpDir = tempfile.TemporaryDirectory()
        logging.warning('TempDir %s', getArchivedImages.tmpDir.name)

    cameraID = cameras[int(len(cameras)*random.random())]['name']
    timeDT = startTimeDT + datetime.timedelta(seconds = random.random()*timeRangeSeconds)
    if minusMinutes:
        prevTimeDT = timeDT + datetime.timedelta(seconds = -60 * minusMinutes)
    else:
        prevTimeDT = timeDT
    files = img_archive.getHpwrenImages(constants['googleServices'], settings, getArchivedImages.tmpDir.name,
                                        constants['camArchives'], cameraID, prevTimeDT, timeDT, minusMinutes or 1)
    # logging.warning('files %s', str(files))
    if not files:
        return (None, None, None, None)
    if minusMinutes:
        if len(files) > 1:
            if files[0] >= files[1]: # files[0] is supposed to be earlier than files[1]
                logging.warning('unexpected file order %s', str(files))
                for file in files:
                    os.remove(file)
                return (None, None, None, None)
            imgDiffPath = genDiffImage(files[1], files[0], minusMinutes)
            os.remove(files[0]) # no longer needed
            parsedName = img_archive.parseFilename(files[1])
            return (cameraID, parsedName['unixTime'], files[1], imgDiffPath)
        else:
            logging.warning('unexpected file count %s', str(files))
            for file in files:
                os.remove(file)
            return (None, None, None, None)
    elif len(files) > 0:
        parsedName = img_archive.parseFilename(files[0])
        return (cameraID, parsedName['unixTime'], files[0], files[0])
    return (None, None, None, None)
getArchivedImages.tmpDir = None


def main():
    optArgs = [
        ["b", "heartbeat", "filename used for heartbeating check"],
        ["c", "collectPositves", "collect positive segments for training data"],
        ["d", "imgDirectory", "Name of the directory containing the images"],
        ["t", "time", "Time breakdown for processing images"],
        ["m", "minusMinutes", "(optional) subtract images from given number of minutes ago"],
        ["r", "restrictType", "Only process images from cameras of given type"],
        ["s", "startTime", "(optional) performs search with modifiedTime > startTime"],
        ["e", "endTime", "(optional) performs search with modifiedTime < endTime"],
    ]
    args = collect_args.collectArgs([], optionalArgs=optArgs, parentParsers=[goog_helper.getParentParser()])
    minusMinutes = int(args.minusMinutes) if args.minusMinutes else 0
    googleServices = goog_helper.getGoogleServices(settings, args)
    dbManager = db_manager.DbManager(sqliteFile=settings.db_file,
                                    psqlHost=settings.psqlHost, psqlDb=settings.psqlDb,
                                    psqlUser=settings.psqlUser, psqlPasswd=settings.psqlPasswd)
    cameras = dbManager.get_sources(activeOnly=True, restrictType=args.restrictType)
    startTimeDT = dateutil.parser.parse(args.startTime) if args.startTime else None
    endTimeDT = dateutil.parser.parse(args.endTime) if args.endTime else None
    timeRangeSeconds = None
    useArchivedImages = False
    camArchives = img_archive.getHpwrenCameraArchives(googleServices['sheet'], settings)
    constants = { # dictionary of constants to reduce parameters in various functions
        'args': args,
        'googleServices': googleServices,
        'camArchives': camArchives,
        'dbManager': dbManager,
    }
    if startTimeDT or endTimeDT:
        assert startTimeDT and endTimeDT
        timeRangeSeconds = (endTimeDT-startTimeDT).total_seconds()
        assert timeRangeSeconds > 0
        assert args.collectPositves
        useArchivedImages = True

    deferredImages = []
    processingTimeTracker = initializeTimeTracker()
    prediction_service = connect_to_prediction_service(settings.server_ip_and_port)
    while True:
        timeStart = time.time()
        if useArchivedImages:
            (cameraID, timestamp, imgPath, classifyImgPath) = \
                getArchivedImages(constants, cameras, startTimeDT, timeRangeSeconds, minusMinutes)
        elif minusMinutes:
            (queueFull, deferredImageInfo) = getDeferrredImgageInfo(deferredImages, processingTimeTracker, minusMinutes,
                                                                    timeStart)
            if not queueFull:  # queue is not full, so add more to queue
                addToDeferredImages(dbManager, cameras, deferredImages)
            if deferredImageInfo:  # we have a deferred image ready to process, now get latest image and subtract
                (cameraID, timestamp, imgPath, classifyImgPath) = \
                    genDiffImageFromDeferred(dbManager, cameras, deferredImageInfo, deferredImages, minusMinutes)
                if not cameraID:
                    continue  # skip to next camera without deleting deferred image which may be reused later
                os.remove(deferredImageInfo['imgPath'])  # no longer needed
            else:
                continue  # in diff mode without deferredImage, nothing more to do
        # elif args.imgDirectory:  unused functionality -- to delete?
        #     (cameraID, timestamp, imgPath, md5) = getNextImageFromDir(args.imgDirectory)
        else: # regular (non diff mode), grab image and process
            (cameraID, timestamp, imgPath, md5) = getNextImage(dbManager, cameras)
            classifyImgPath = imgPath
        if not cameraID:
            continue # skip to next camera
        image_crops, segment_infos = segmentImage(classifyImgPath)
        timeFetch = time.time()

        segments = classify(image_crops, segment_infos, prediction_service)
        timeClassify = time.time()
        recordFilterReport(constants, cameraID, timestamp, classifyImgPath, imgPath, segments, minusMinutes, googleServices['drive'], useArchivedImages)
        timePost = time.time()
        updateTimeTracker(processingTimeTracker, timePost - timeStart)
        if args.time:
            logging.warning('Timings: fetch=%.2f, classify=%.2f, post=%.2f',
                timeFetch-timeStart, timeClassify-timeFetch, timePost-timeClassify)


if __name__== "__main__":
    main()
