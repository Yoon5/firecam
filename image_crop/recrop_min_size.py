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

Reads data from csv export of Fuego Cropped Images sheet to find the original
entire image name and the manually selected rectangular bounding box. Then
downloads the entire image and recrops it by increasing size of the rectangle
by given growRatio and to exceed the specified minimums.  Also, very large
images are discarded (controlled by throwSize)

Optionally, for debuggins shows the boxes on screen (TODO: refactor display code)

"""
import os
import csv
import tkinter as tk
from PIL import Image, ImageTk

import sys
import settings
sys.path.insert(0, settings.fuegoRoot + '/lib')
import collect_args
import goog_helper
import rect_to_squares

def imageDisplay(imgOrig, title=''):
    rootTk = tk.Tk()
    rootTk.title('Fuego: ' + title)
    screen_width = rootTk.winfo_screenwidth() - 100
    screen_height = rootTk.winfo_screenheight() - 100

    print("Image:", (imgOrig.size[0], imgOrig.size[1]), ", Screen:", (screen_width, screen_height))
    scaleX = min(screen_width/imgOrig.size[0], 1)
    scaleY = min(screen_height/imgOrig.size[1], 1)
    scaleFactor = min(scaleX, scaleY)
    print('scale', scaleFactor, scaleX, scaleY)
    scaledImg = imgOrig
    if (scaleFactor != 1):
        scaledImg = imgOrig.resize((int(imgOrig.size[0]*scaleFactor), int(imgOrig.size[1]*scaleFactor)), Image.ANTIALIAS)
    imgPhoto = ImageTk.PhotoImage(scaledImg)
    canvasTk = tk.Canvas(rootTk, width=imgPhoto.width(), height=imgPhoto.height(), bg="light yellow")
    canvasTk.config(highlightthickness=0)

    aff=canvasTk.create_image(0, 0, anchor='nw', image=imgPhoto)
    canvasTk.focus_set()
    canvasTk.pack(side='left', expand='yes', fill='both')

    return (rootTk, canvasTk, imgPhoto, scaleFactor)


def buttonClick(event):
    exit()

# use multiple colors to make it slightly easier to see the overlapping boxes
colors = ['red', 'blue']

def displayImageWithScores(imgOrig, segments):
    (rootTk, canvasTk, imgPhoto, scaleFactor) = imageDisplay(imgOrig)
    canvasTk.bind("<Button-1>", buttonClick)
    canvasTk.bind("<Button-2>", buttonClick)
    canvasTk.bind("<Button-3> ", buttonClick)
    for counter, coords in enumerate(segments):
        (sx0, sy0, sx1, sy1) = coords
        offset = ((counter%2) - 0.5)*2
        x0 = sx0*scaleFactor + offset
        y0 = sy0*scaleFactor + offset
        x1 = sx1*scaleFactor + offset
        y1 = sy1*scaleFactor + offset
        color = colors[counter % len(colors)]
        canvasTk.create_rectangle(x0, y0, x1, y1, outline=color, width=2)
    rootTk.mainloop()


def getCameraDir(service, cameraCache, fileName):
    parsed = goog_helper.parseFilename(fileName)
    cameraID = parsed['cameraID']
    dirID = cameraCache.get(cameraID)
    if not dirID:
        (dirID, dirName) = goog_helper.getDirForClassCamera(service, settings.IMG_CLASSES, 'smoke', cameraID)
        cameraCache[cameraID] = dirID
    return dirID


def expandRange(val0, val1, minimumDiff, growRatio, minLimit, maxLimit):
    """Expand the image dimension range

    Inceases the min/max of the range by growRatio while maintaining
    the same center. Also, ensures the range is at least minimumDiff.
    Finally, limits the increases to ensure values are still within
    the entire range of the image (minLimit, maxLimit)

    Args:
        val0 (int): starting (minimum) value of the input range
        val1 (int): ending (maximum) value of the input range
        minimumDiff (int): mimimum size of the output range
        growRatio (float): ratio (expected > 1) to expand the range by
        minLimit (int): absolute minimum value of the output range
        maxLimit (int): absolute maximum value of the output range

    Returns:
        (int, int): start, end of the adjusted range
    """
    val0 = max(val0, minLimit)
    val1 = min(val1, maxLimit)
    diff = val1 - val0
    center = val0 + int(diff/2)
    minimumDiff = max(minimumDiff, int(diff*growRatio))
    if diff < minimumDiff:
        if (center - int(minimumDiff/2)) < minLimit:   # left edge limited
            val0 = minLimit
            val1 = min(val0 + minimumDiff, maxLimit)
        elif (center + int(minimumDiff/2)) > maxLimit: # right edge limited
            val1 = maxLimit
            val0 = max(val1 - minimumDiff, minLimit)
        else:                                          # unlimited
            val0 = center - int(minimumDiff/2)
            val1 = min(val0 + minimumDiff, maxLimit)
    return (val0, val1)


def main():
    reqArgs = [
        ["o", "outputDir", "local directory to save images and segments"],
        ["i", "inputCsv", "csvfile with contents of Fuego Cropped Images"],
    ]
    optArgs = [
        ["s", "startRow", "starting row"],
        ["e", "endRow", "ending row"],
        ["d", "display", "(optional) specify any value to display image and boxes"],
        ["x", "minDiffX", "(optional) override default minDiffX of 100"],
        ["y", "minDiffY", "(optional) override default minDiffY of 100"],
        ["t", "throwSize", "(optional) override default throw away size of 500x500"],
        ["g", "growRatio", "(optional) override default grow ratio of 1.2"],
    ]
    args = collect_args.collectArgs(reqArgs, optionalArgs=optArgs, parentParsers=[goog_helper.getParentParser()])
    startRow = int(args.startRow) if args.startRow else 0
    endRow = int(args.endRow) if args.endRow else 1e9
    minDiffX = int(args.minDiffX) if args.minDiffX else 100
    minDiffY = int(args.minDiffY) if args.minDiffY else 100
    throwSize = int(args.throwSize) if args.throwSize else 500
    growRatio = float(args.growRatio) if args.growRatio else 1.2

    googleServices = goog_helper.getGoogleServices(settings, args)
    cameraCache = {}
    with open(args.inputCsv) as csvFile:
        csvreader = csv.reader(csvFile)
        for (rowIndex, csvRow) in enumerate(csvreader):
            if rowIndex < startRow:
                continue
            if rowIndex > endRow:
                print('Reached end row', rowIndex, endRow)
                exit(0)
            [cropName, minX, minY, maxX, maxY, fileName] = csvRow[:6]
            minX = int(minX)
            minY = int(minY)
            maxX = int(maxX)
            maxY = int(maxY)
            oldCoords = (minX, minY, maxX, maxY)
            if ((maxX - minX) > throwSize) and ((maxY - minY) > throwSize):
                print('Skip large image', fileName)
                continue
            dirID = getCameraDir(googleServices['drive'], cameraCache, fileName)
            localFilePath = os.path.join(args.outputDir, fileName)
            print('local', localFilePath)
            if not os.path.isfile(localFilePath):
                print('download', fileName)
                goog_helper.downloadFile(googleServices['drive'], dirID, fileName, localFilePath)
            imgOrig = Image.open(localFilePath)
            (newMinX, newMaxX) = expandRange(minX, maxX, minDiffX, growRatio, 0, imgOrig.size[0])
            (newMinY, newMaxY) = expandRange(minY, maxY, minDiffY, growRatio, 0, imgOrig.size[1])
            newCoords = (newMinX, newMinY, newMaxX, newMaxY)
            # XXXX - save work if old=new?
            print('coords old,new', oldCoords, newCoords)
            imgNameNoExt = str(os.path.splitext(fileName)[0])
            cropImgName = imgNameNoExt + '_Crop_' + 'x'.join(list(map(lambda x: str(x), newCoords))) + '.jpg'
            cropImgPath = os.path.join(args.outputDir, 'cropped', cropImgName)
            cropped_img = imgOrig.crop(newCoords)
            cropped_img.save(cropImgPath, format='JPEG')
            flipped_img = cropped_img.transpose(Image.FLIP_LEFT_RIGHT)
            flipImgName = imgNameNoExt + '_Crop_' + 'x'.join(list(map(lambda x: str(x), newCoords))) + '_Flip.jpg'
            flipImgPath = os.path.join(args.outputDir, 'cropped', flipImgName)
            flipped_img.save(flipImgPath, format='JPEG')
            print('Processed row: %s, file: %s' % (rowIndex, fileName))
            if args.display:
                displayCoords = [oldCoords, newCoords]
                displayImageWithScores(imgOrig, displayCoords)
                imageDisplay(imgOrig)

if __name__=="__main__":
    main()