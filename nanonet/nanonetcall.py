#!/usr/bin/env python
import argparse
import json
import os
import re
import math
import sys
import shutil
import tempfile
import timeit
import subprocess
import pkg_resources
import itertools
import numpy as np
import pyopencl as cl

from nanonet import decoding, nn
from nanonet.fast5 import Fast5, iterate_fast5
from nanonet.util import random_string, conf_line, FastaWrite, tang_imap, all_nmers, kmers_to_sequence, kmer_overlap, AddFields
from nanonet.cmdargs import FileExist, CheckCPU, AutoBool
from nanonet.features import make_basecall_input_multi

import warnings
warnings.simplefilter("ignore")


__fast5_analysis_name__ = 'Basecall_RNN_1D'
__fast5_section_name__ = 'BaseCalled_{}'
__ETA__ = 1e-300


def get_parser():
    parser = argparse.ArgumentParser(
        description="""A simple ANN 3-mer basecaller, works only on HMM basecall mapped data.""",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    parser.add_argument("input", action=FileExist,
        help="A path to fast5 files.")
    parser.add_argument("--watch", default=None, type=int,
        help="Switch to watching folder, argument value used as timeout period.")
    parser.add_argument("--section", default=None, choices=('template', 'complement'),
        help="Section of read for which to produce basecalls, will override that stored in model file.")
    parser.add_argument("--event_detect", default=True, action=AutoBool,
        help="Perform event detection, else use existing event data")

    parser.add_argument("--output", type=str,
        help="Output name, output will be in fasta format.")
    parser.add_argument("--write_fast5", action=AutoBool, default=False,
        help="Write datasets to .fast5.")
    parser.add_argument("--strand_list", default=None, action=FileExist,
        help="List of reads to process.")
    parser.add_argument("--limit", default=None, type=int,
        help="Limit the number of input for processing.")
    parser.add_argument("--min_len", default=500, type=int,
        help="Min. read length (events) to basecall.")
    parser.add_argument("--max_len", default=15000, type=int,
        help="Max. read length (events) to basecall.")
    
    parser.add_argument("--model", type=str, action=FileExist,
        default=pkg_resources.resource_filename('nanonet', 'data/default_template.npy'),
        help="Trained ANN.")
    parser.add_argument("--jobs", default=1, type=int,
        help="No of decoding jobs to run in parallel.")

    parser.add_argument("--trans", type=float, nargs=3, default=None,
        metavar=('stay', 'step', 'skip'), help='Base transition probabilities')

    parser.add_argument("--opencl", action=AutoBool, default=False,
        help="Offload computation to GPU using OpenCL.")
    parser.add_argument("--opencl_input_files", default=1, type=int,
        help="Number of input fast5 files to be processed simultaneously. For GPU devices that support concurrent kernel execution.")
    parser.add_argument("--list_platforms", action=AutoBool, default=False,
        help="Output list of available OpenCL GPU platforms.")
    parser.add_argument("--platforms", nargs="+", type=str,
        help="List of OpenCL GPU platforms and devices to be used in a format VENDOR:DEVICE space separated, i.e. --platforms nvidia:0 amd:0 amd:1.")

    return parser

class process_attr:
    def __init__(self, fast5_files, use_opencl=False, vendor=None, device_id=0):
        self.fast5_files = fast5_files
        self.use_opencl = use_opencl
        self.vendor = vendor
        self.device_id = device_id

def list_opencl_platforms():
    print('\n' + '=' * 60 + '\nOpenCL Platforms and Devices')
    platforms = [p for p in cl.get_platforms() if p.get_devices(device_type=cl.device_type.GPU)]
    for platform in platforms:
        print('=' * 60)
        print('Platform - Name:  ' + platform.name)
        print('Platform - Vendor:  ' + platform.vendor)
        print('Platform - Version:  ' + platform.version)
        for device in platform.get_devices(device_type=cl.device_type.GPU):  # Print each device per-platform
            print('    ' + '-' * 56)
            print('    Device - Name:  ' + device.name)
            print('    Device - Type:  ' + cl.device_type.to_string(device.type))
            print('    Device - Max Clock Speed:  {0} Mhz'.format(device.max_clock_frequency))
            print('    Device - Compute Units:  {0}'.format(device.max_compute_units))
            print('    Device - Local Memory:  {0:.0f} KB'.format(device.local_mem_size/1024))
            print('    Device - Constant Memory:  {0:.0f} KB'.format(device.max_constant_buffer_size/1024))
            print('    Device - Global Memory: {0:.0f} GB'.format(device.global_mem_size/1073741824.0))
    print('\n')
        
def process_read(modelfile, pa, min_prob=1e-5, trans=None, post_only=False, write_events=True, **kwargs):
    """Run neural network over a set of fast5 files

    :param modelfile: neural network specification.
    :param fast5: read file to process
    :param post_only: return only the posterior matrix
    :param **kwargs: kwargs of make_basecall_input_multi
    """
    t0 = timeit.default_timer()
    network = np.load(modelfile).item()
    t1 = timeit.default_timer()
    load_time = t1 - t0

    kwargs['window'] = network.meta['window']
    fast5_list = pa.fast5_files
    use_opencl = pa.use_opencl
    
    post_list = []
    features_list = []
    events_list = []
    name_list = []
    
    feature_time_list = []
    network_time_list = []

    for fast5 in fast5_list:
        t1 = timeit.default_timer()
        try:
            it = make_basecall_input_multi((fast5,), **kwargs)
            if write_events:
                name, features, events = it.next()
                events_list.append(events)
            else:
                name, features, _ = it.next()
            features_list.append(features.astype(nn.tang_nn_type))
            name_list.append(name)
        except Exception as e:
            return None
        t2 = timeit.default_timer()
        feature_time_list.append(t2 - t1)
    
        if not use_opencl:
            post = network.run(features.astype(nn.tang_nn_type))
            post_list.append(post)
            t3 = timeit.default_timer()
            network_time_list.append(t3 - t2)
            
    ctx = None
    queue_list = []
    max_workgroup_size = 256        
    t2 = timeit.default_timer()
    if use_opencl:
        platforms = [p for p in cl.get_platforms() if p.get_devices(device_type=cl.device_type.GPU) and pa.vendor.lower() in p.get_info(cl.platform_info.NAME).lower()]
        platform = platforms[0]
        devices = platform.get_devices(device_type=cl.device_type.GPU)
        device = devices[pa.device_id]
        max_workgroup_size = device.get_info(cl.device_info.MAX_WORK_GROUP_SIZE)
        ctx = cl.Context([device]) 
        for x in xrange(len(features_list)):
            queue_list.append(cl.CommandQueue(ctx))
        post_list = network.run(features_list, ctx, queue_list)
        t3 = timeit.default_timer()
        for x in xrange(len(features_list)):
            network_time_list.append((t3 - t2)/len(features_list))

    
    trans_copy = trans
    trans_list = []
    good_events_list = []
    t3 = timeit.default_timer()
    for x in xrange(len(fast5_list)):
        post = post_list[x]
        trans = trans_copy
        
        kmers = network.meta['kmers']
        # Do we have an XXX kmer? Strip out events where XXX most likely,
        #    and XXX states entirely
        if kmers[-1] == 'X'*len(kmers[-1]):
            bad_kmer = post.shape[1] - 1
            max_call = np.argmax(post, axis=1)
            good_events = (max_call != bad_kmer)
            good_events_list.append(good_events)
            post = post[good_events]
            post = post[:, :-1]
    
        weights = np.sum(post, axis=1).reshape((-1,1))
        post /= weights
        if post_only:
            return post
    
        post = min_prob + (1.0 - min_prob) * post
        post_list[x] = post
        trans = decoding.estimate_transitions(post, trans=trans)
        trans_list.append(np.log(__ETA__ + trans))
    
    if use_opencl:
        score_list, states_list = decoding.decode_profile_opencl(ctx, queue_list, post_list, trans_list=trans_list, log=False, max_workgroup_size=max_workgroup_size)
    else:
        score_list = [None] * len(fast5_list)
        states_list = [None] * len(fast5_list)
        for x in xrange(len(fast5_list)):
            score_list[x], states_list[x] = decoding.decode_profile(post_list[x], trans=trans_list[x], log=False)
            
    # Form basecall
    kmer_path_list = []
    seq_list = []
    for x in xrange(len(fast5_list)):
        kmer_path = [kmers[i] for i in states_list[x]]
        seq = kmers_to_sequence(kmer_path)
        kmer_path_list.append(kmer_path)
        seq_list.append(seq)

    decode_time = timeit.default_timer() - t3
    decode_time_list = []
    for x in xrange(len(fast5_list)):
        decode_time_list.append(decode_time/len(fast5_list))
    
    ret = []
    # Write events table
    if write_events:
        for x in xrange(len(fast5_list)):
            t4 = timeit.default_timer()
            events = events_list[x]
            name = name_list[x]
            post = post_list[x]
            kmer_path = kmer_path_list[x]
            seq = seq_list[x]
            states = states_list[x]
            score = score_list[x]
            good_events = good_events_list[x] 
            adder = AddFields(events[good_events])
            adder.add('model_state', kmer_path,
                dtype='>S{}'.format(len(kmers[0])))
            adder.add('p_model_state', np.fromiter(
                (post[i,j] for i,j in itertools.izip(xrange(len(post)), states)),
                dtype=float, count=len(post)))
            adder.add('mp_model_state', np.fromiter(
                (kmers[i] for i in np.argmax(post, axis=1)),
                dtype='>S{}'.format(len(kmers[0])), count=len(post)))
            adder.add('p_mp_model_state', np.max(post, axis=1))
            adder.add('move', np.array(kmer_overlap(kmer_path)), dtype=int)
    
            mid = len(kmers[0]) / 2
            bases = set(''.join(kmers)) - set('X')
            for base in bases:
                cols = np.fromiter((k[mid] == base for k in kmers),
                    dtype=bool, count=len(kmers))
                adder.add('p_{}'.format(base), np.sum(post[:, cols], axis=1), dtype=float)
    
            events = adder.finalize()
    
            with Fast5(fast5, 'a') as fh:
               base = fh._join_path(
                   fh.get_analysis_new(__fast5_analysis_name__),
                   __fast5_section_name__.format(kwargs['section']))
               fh._add_event_table(events, fh._join_path(base, 'Events'))
               try:
                   name = fh.get_read(group=True).attrs['read_id']
               except:
                   pass # filename inherited from above
               fh._add_string_dataset(
                   '@{}\n{}\n+\n{}\n'.format(name, seq, '!'*len(seq)),
                   fh._join_path(base, 'Fastq'))
 
            write_time = timeit.default_timer() - t4
            ret.append((name, seq, score, len(post), (feature_time_list[x], load_time, network_time_list[x], decode_time_list[x], write_time)))
        
    return ret

def main():
    if len(sys.argv) == 1:
        sys.argv.append("-h")
    args = get_parser().parse_args()
    
    if args.list_platforms:
        list_opencl_platforms() 
        sys.exit(0)
        
    modelfile  = os.path.abspath(args.model)
    if args.section is None:
        try:
            args.section = np.load(modelfile).item().meta['section']
        except:
            sys.stderr.write("No 'section' found in modelfile, try specifying --section.\n")
            sys.exit(1)
                 
    fix_args = [
        modelfile
    ]
    fix_kwargs = {
        'min_len':args.min_len,
        'max_len':args.max_len,
        'section':args.section,
        'event_detect':args.event_detect
    }

    files_pattern = [1] * args.jobs
    if args.opencl:
        for x in xrange(len(args.platforms)):
            files_pattern[x] = args.opencl_input_files
            
    #TODO: handle case where there are pre-existing files.
    if args.watch is not None:
        # An optional component
        from nanonet.watcher import Fast5Watcher
        fast5_files = Fast5Watcher(args.input, timeout=args.watch)
    else:                                                                                                                                 
        fast5_files = iterate_fast5(args.input, paths=True, strand_list=args.strand_list, limit=args.limit, files_group_pattern=files_pattern)

    pa_list = []
    for i,ff in enumerate(fast5_files):
        if args.opencl and i % args.jobs in xrange(len(args.platforms)):
            vendor,device_id = args.platforms[i%args.jobs].split(':')
            pa_list.append(process_attr(ff, use_opencl=True, vendor=vendor, device_id=int(device_id)))
        else:
            pa_list.append(process_attr(ff))

    t0 = timeit.default_timer()
    n_reads = 0
    n_bases = 0
    n_events = 0
    timings = [0.0, 0.0, 0.0, 0.0, 0.0]
    with FastaWrite(args.output) as fasta:
        for ret in tang_imap(process_read, pa_list, fix_args=fix_args, fix_kwargs=fix_kwargs, threads=args.jobs):
            if ret is None:
                continue
            for result in ret:
                name, basecall, _, n_ev, time = result
                fasta.write(*(name, basecall))
                n_reads += 1
                n_bases += len(basecall)
                n_events += n_ev
                timings = [x + y for x, y in zip(timings, time)]
    t1 = timeit.default_timer()
    sys.stderr.write('Basecalled {} reads ({} bases, {} events) in {}s (wall time)\n'.format(n_reads, n_bases, n_events, t1 - t0))
    if n_reads > 0:
        feature, load, network, decoding, events_writing = timings
        feature /= args.jobs
        load /= args.jobs
        network /= args.jobs
        decoding /= args.jobs
        events_writing /= args.jobs
        sys.stderr.write(
            'Profiling\n---------\n'
            'Feature generation: {}\n'
            'Load network: {}\n'
            'Run network: {} ({} kb/s, {} kev/s)\n'
            'Decoding: {} ({} kb/s, {} kev/s)\n'
            'Write events: {}\n'.format(
                feature, load,
                network, n_bases/1000/network, n_events/1000/network,
                decoding, n_bases/1000/decoding, n_events/1000/decoding,
                events_writing
            )
        )


if __name__ == "__main__":
    main()
