#!/usr/bin/env python

"""
This module defines the general analysis workflow.

"""

import copy
import os
import re
import shutil
import sys
import time
import traceback
import yaml

import numpy as np

from distutils import archive_util
from pandas import concat, read_pickle

import log
import parallel
import read

MODES = ()
try:
    import phase.workflow
    MODES += ('phase', )
except ImportError as e:
    print 'phase:', e
try:
    import fluor.workflow
    MODES += ('fluor', )
except ImportError as e:
    pass
#     print 'fluor:', e

__author__ = 'Charlie Wright'
__email__ = 'charles.s.wright@gmail.com'

def cls():
    """
    Clear the terminal display.

    """
    os.system('cls' if os.name=='nt' else 'clear')

def countdown(msg='', tTotal=11):
    """
    Display seconds countdown on the terminal screen.

    kwargs:
        msg (str): display message
        tTotal (int): seconds in countdown

    """
    tElapsed = 0
    tStart = time.time()
    while tElapsed < tTotal:
        cls()
        print msg, str(int(tTotal - tElapsed)), '...'
        time.sleep(0.1)
        tElapsed = time.time() - tStart

def loadinputs(input_dir):
    """
    Load analysis input parameters from .txt file.

    args:
        input_dir (path): experiment analysis directory containing .yaml file

    returns:
        dict: input parameters

    """
    input_file = os.path.join(input_dir, 'params.yaml')
    try:
        # Load experiment-specific parameters from file
        inputs = yaml.load(open(input_file, 'r'))
    except IOError:
        # Load the default parameters and change the experiment name
        inputs = yaml.load(open(os.path.join(os.path.dirname(__file__),
                                'defaults.yaml'), 'r'))
        for k in inputs['name'].keys():
            inputs['name'][k] = os.path.basename(input_dir)

        # Make experiment directory as necessary, and save parameters to file
        read.rmkdir(input_dir)
        yaml.dump(inputs, open(input_file, 'w'),
                  explicit_start=True, default_flow_style=False)
        msg = '''
        Loaded default parameters to %s.
        Exit now and edit this file to use non-default values.
        Starting automatic analysis in''' % input_file
        countdown(msg)
        cls()

    # Expand all directories
    for k, v in inputs['paths'].iteritems():
        root, pattern = os.path.split(os.path.abspath(os.path.expanduser(v)))
        try:
            sub_dir = read.listdirs(root, pattern).next()
        except StopIteration:
            sub_dir = pattern
        inputs['paths'][k] = os.path.join(root, sub_dir)
    return inputs

def setpositions(inputs, mode='raw_data'):
    """
    Convert list of regex position patterns to full list of positions.

    args:
        inputs (dict): input parameters

    kwargs:
        mode (str): 'raw_data' or 'analyses'

    returns:
        inputs (dict): parameters input with updated positions field

    """
    positions = set()
    input_dir = os.path.join(inputs['paths'][mode], inputs['name'][mode])
    for v in os.listdir(input_dir):
        [positions.add(v) for p in inputs['positions'] if re.match(p, v)
         and os.path.isdir(os.path.join(input_dir, v))]
    inputs['positions'] = sorted(positions)
    return inputs

def preeditblock(input_dir, output_dir, modes, params):
    for m in modes:
        d = params[m]
        for f in d['segment']['file_list']:
            input_file = os.path.join(input_dir, f)
            eval('%s.workflow.preeditimage(input_file, output_dir, d)' % m)

def trackblock(input_dir, output_file, params):
    phase.workflow.trackblock(input_dir, output_file, params)

def stitchblocks(input_dirs, params):
    phase.workflow.stitchblocks(input_dirs, params)

def collateblocks(input_dirs, output_file, params):
    phase.workflow.collateblocks(input_dirs, output_file, params)

def preeditmovie(expt_raw_data_dir, expt_analyses_dir, positions, params):
    """
    Automated steps to perform prior to editing.

    """
    expt = os.path.basename(expt_analyses_dir)
    g = params['general']

    # First load or create log files for each position
    log.main(expt_raw_data_dir, expt_analyses_dir, positions, g['write_mode'])

    # Execute each position in succession
    for p in positions:
        # Update the terminal display
        read.updatelog(expt, p, 'preedit')
        print 'start: ' + time.asctime()

        posn_raw_data_dir = os.path.join(expt_raw_data_dir, p)
        posn_analyses_dir = os.path.join(expt_analyses_dir, p)

        # Segmented files will be saved to a temporary directory
        temp_dir = os.path.join(posn_analyses_dir, 'temp')
        if g['write_mode'] == 0:
            read.rmkdir(temp_dir)
        else:
            read.cmkdir(temp_dir)

        # Pad with default parameters, and find frames to process
        frame_start, frame_stop = float('inf'), 0.
        for m in MODES:
            d = params[m]

            # Pad with default parameters as necessary
            d = eval('%s.workflow.fillparams(d)' % m)

            # Find all .tif images of specified type in the given directory
            d['segment']['file_list'] = []
            for f in read.listfiles(posn_raw_data_dir, d['segment']['pattern']):
                j = read.getframenum(f, d['segment']['pattern'])
                if g['frame_range'][0] <= j < g['frame_range'][1]:
                    frame_start = min(frame_start, j)
                    frame_stop = max(frame_stop, j)
                    d['segment']['file_list'].append(f)
            frame_stop += 1

        # Create arguments for parallel processing
        args = [(posn_raw_data_dir, temp_dir,
                 MODES, copy.deepcopy(params)) for _ in range(g['num_procs'])]
        for i, a in enumerate(args):
            for m in a[2]:
                v = a[3][m]['segment']['file_list']
                v = v[i::g['num_procs']]
                a[3][m]['segment']['file_list'] = v
        sum([len(a[3]['phase']['segment']['file_list']) for a in args])

        # Process each block of frames in parallel
        parallel.main(preeditblock, args, g['num_procs'])
        print 'sextract: ' + time.asctime()

        # Archive the output files into .zip files, then delete each .tif
        num_tifs = frame_stop - frame_start
        num_digits = int(np.ceil(np.log10(num_tifs + 1)))

        # Create new set of directories with pre-specified block size
        frames = range(frame_start, frame_stop-1, g['block_size'])
        frames.append(frame_stop)
        block_frames = zip(frames[:-1], frames[1:])

        # Make directories to hold files, named according to frames
        read.cmkdir(os.path.join(posn_analyses_dir, 'blocks'))
        block_dirs = []
        for j1, j2 in block_frames:
            strs = [str(v).zfill(num_digits) for v in (j1, j2)]
            v = os.path.join(posn_analyses_dir, 'blocks',
                             'frame{}-{}'.format(*strs))
            os.mkdir(v)
            block_dirs.append(v)

        for m in MODES:
            # The segmented .tif files will be stored in a .zip file
            zip_name = m.capitalize() + 'Segment'
            [read.cmkdir(os.path.join(v, zip_name)) for v in block_dirs]

            # Find all segmented .tif images and transfer to the new directories
            d = params[m]
            for f in read.listfiles(temp_dir, d['segment']['pattern']):
                j = read.getframenum(f, d['segment']['pattern'])
                for i, (j1, j2) in enumerate(block_frames):
                    if j1 <= j < j2:
                        old_name = os.path.join(temp_dir, f)
                        zip_dir = os.path.join(block_dirs[i], zip_name)
                        shutil.move(old_name, zip_dir)

            # Zip each directory of segmented .tif files
            old_dir = os.path.abspath(os.curdir)
            for v in block_dirs:
                os.chdir(v)
                archive_util.make_zipfile(zip_name, zip_name)
                shutil.rmtree(zip_name)
                os.chdir(old_dir)

            # Make temporary directories for data outputs
            dat_name = m.capitalize() + 'Data'
            [read.cmkdir(os.path.join(v, dat_name)) for v in block_dirs]

            # Find all analyzed .dat files and transfer to the new directories
            f, e = os.path.splitext(d['segment']['pattern'])
            dat_pattern = (f + '.pickle' + e[4:])
            for f in read.listfiles(temp_dir, dat_pattern):
                j = read.getframenum(f, dat_pattern)
                for i, (j1, j2) in enumerate(block_frames):
                    if j1 <= j < j2:
                        # Transfer each frame to the correct block
                        old_name = os.path.join(temp_dir, f)
                        dat_dir = os.path.join(block_dirs[i], dat_name)
                        shutil.move(old_name, dat_dir)

            # Concatenate each set of files into a DataFrame for each parameter
            for v in block_dirs:
                dat_dir = os.path.join(v, dat_name)
                data = []
                for u in os.listdir(dat_dir):
                    dat_file = os.path.join(dat_dir, u)
                    try:
                        d = read_pickle(dat_file)
                    except:
                        # traceback.print_exc()
                        pass
                    data.append(d)
                df = concat(data)
                df = df.reindex(sorted(df.index))
                for c in df.columns:
                    df[c].to_pickle(os.path.join(v, c + '.pickle'))
                shutil.rmtree(dat_dir)
        print 'shuffle: ' + time.asctime()

        # Delete all temporary files
        shutil.rmtree(temp_dir)
        '''
        block_dirs = [os.path.join(posn_analyses_dir, 'blocks', v) for v in
                      os.listdir(os.path.join(posn_analyses_dir, 'blocks'))
                      if 'frame' in v]
        '''
        # Track the blocks in parallel
        args = []
        for v in block_dirs:
            output_file = os.path.join(v, 'Trace.pickle')
            if os.path.isfile(output_file):
                os.remove(output_file)
            args.append((v, output_file, params['phase']['track']))
        parallel.main(trackblock, args, g['num_procs'])
        print 'track: ' + time.asctime()

        # Stitch independently-tracked trajectories together
        stitchblocks(block_dirs, params['phase']['track'])
        print 'stitch: ' + time.asctime()


        # Collate the data for manual editing
        output_file = os.path.join(posn_analyses_dir, 'edits.pickle')
        collateblocks(block_dirs, output_file, params['phase']['collate'])
        print 'collate: ' + time.asctime()

        # Update the experiment log file
        read.updatelog(expt, p, 'preedit', expt_analyses_dir)
        print 'final: ' + time.asctime()

def editmovie(expt_raw_data_dir, expt_analyses_dir, positions):
    """
    Interactive, manual editing.

    """
    phase.workflow.editmovie(expt_raw_data_dir, expt_analyses_dir, positions)

def posteditblock(input_dir, params):
    phase.workflow.posteditblock(input_dir, params)

def posteditmovie(expt_raw_data_dir, expt_analyses_dir, positions, params):
    """
    Automated, post-editing analysis.

    """
    expt = os.path.basename(expt_analyses_dir)

    # Execute each position in succession
    for p in positions:
        # Update the terminal display
        read.updatelog(expt, p, 'postedit')

        # Names of blocks directories containing each frame range
        posn_analyses_dir = os.path.join(expt_analyses_dir, p)
        block_dirs = [os.path.join(posn_analyses_dir, 'blocks', v) for v in
                      os.listdir(os.path.join(posn_analyses_dir, 'blocks'))
                      if 'frame' in v]

        # Track the blocks in parallel
        args = [(v, params['general']) for v in block_dirs]
        parallel.main(posteditblock, args, params['general']['num_procs'])

        # Update the experiment log file
        read.updatelog(expt, p, 'postedit', expt_analyses_dir)

def main(input_dir, mode='preedit'):
    inputs = loadinputs(input_dir)
    expt_raw_data_dir = os.path.join(inputs['paths']['raw_data'],
                                     inputs['name']['raw_data'])
    expt_analyses_dir = os.path.join(inputs['paths']['analyses'],
                                     inputs['name']['analyses'])
    if mode in (0, 'preedit'):
        inputs = setpositions(inputs, 'raw_data')
        preeditmovie(expt_raw_data_dir, expt_analyses_dir,
                     inputs['positions'], inputs['parameters'])
    elif mode in (1, 'edit'):
        inputs = setpositions(inputs, 'analyses')
        editmovie(expt_raw_data_dir, expt_analyses_dir, inputs['positions'])
    elif mode in (2, 'postedit'):
        inputs = setpositions(inputs, 'analyses')
        posteditmovie(expt_raw_data_dir, expt_analyses_dir,
                     inputs['positions'], inputs['parameters'])

if __name__ == "__main__":
    if len(sys.argv) > 1:
        input_dir = sys.argv[1]
    else:
        raise IndexError('list index out of range')
        #input_dir = '~/Dropbox/Scherer/Caulobacter/Code/example/Analyses/2013-01-12'
    if len(sys.argv) > 2:
        mode = sys.argv[2]
    else:
        mode = 'preedit'
    main(os.path.expanduser(input_dir), mode)