#! /usr/bin/env python2
#
# This file is part of khmer, http://github.com/ged-lab/khmer/, and is
# Copyright (C) Michigan State University, 2009-2014. It is licensed under
# the three-clause BSD license; see doc/LICENSE.txt.
# Contact: khmer-project@idyll.org
#
"""
Trim sequences at k-mers of the given abundance, using a streaming algorithm.
Output sequences will be placed in 'infile.abundtrim'.

% python sandbox/trim-low-abund.py [ <data1> [ <data2> [ ... ] ] ]

Use -h for parameter help.

TODO: load/save counting table.
TODO: reference appropriate preprint.
"""
import sys
import screed
import os
import khmer
import argparse
import tempfile
import shutil
import textwrap
from screed.screedRecord import _screed_record_dict

from khmer.utils import (write_record, write_record_pair, broken_paired_reader)


DEFAULT_NORMALIZE_LIMIT = 20
DEFAULT_CUTOFF = 2

DEFAULT_K = 32
DEFAULT_N_HT = 4
DEFAULT_MIN_HASHSIZE = 1e6

# see Zhang et al., http://arxiv.org/abs/1309.2975
MAX_FALSE_POSITIVE_RATE = 0.8


def trim_record(read, trim_at):
    new_read = _screed_record_dict()
    new_read.name = read.name
    new_read.sequence = read.sequence[:trim_at]
    if hasattr(read, 'accuracy'):
        new_read.accuracy = read.accuracy[:trim_at]

    return new_read


def get_parser():
    epilog = """
    The output is one file for each input file, <input file>.abundtrim, placed
    in the current directory.  This output contains the input sequences
    trimmed at low-abundance k-mers.

    The ``-V/--variable-coverage`` parameter will, if specified,
    prevent elimination of low-abundance reads by only trimming
    low-abundance k-mers from high-abundance reads; use this for
    non-genomic data sets that may have variable coverage.

    Note that the output reads will not necessarily be in the same order
    as the reads in the input files; if this is an important consideration,
    use ``load-into-counting.py`` and ``filter-abund.py``.  However, read
    pairs will be kept together, in "broken-paired" format; you can use
    ``extract-paired-reads.py`` to extract read pairs and orphans.

    Example::

        trim-low-abund.py -x 5e7 -k 20 -C 2 data/100k-filtered.fa
    """

    parser = argparse.ArgumentParser(
        description='Trim low-abundance k-mers using a streaming algorithm.',
        epilog=textwrap.dedent(epilog))

    env_ksize = os.environ.get('KHMER_KSIZE', DEFAULT_K)
    env_n_hashes = os.environ.get('KHMER_N_HASHES', DEFAULT_N_HT)
    env_hashsize = os.environ.get('KHMER_MIN_HASHSIZE', DEFAULT_MIN_HASHSIZE)

    parser.add_argument('--ksize', '-k', type=int, dest='ksize',
                        default=env_ksize,
                        help='k-mer size to use')
    parser.add_argument('--n_hashes', '-N', type=int, dest='n_hashes',
                        default=env_n_hashes,
                        help='number of hash tables to use')
    parser.add_argument('--hashsize', '-x', type=float, dest='min_hashsize',
                        default=env_hashsize,
                        help='lower bound on hashsize to use')

    parser.add_argument('--cutoff', '-C', type=int, dest='abund_cutoff',
                        help='remove k-mers below this abundance',
                        default=DEFAULT_CUTOFF)

    parser.add_argument('--normalize-to', '-Z', type=int, dest='normalize_to',
                        help='base cutoff on this median k-mer abundance',
                        default=DEFAULT_NORMALIZE_LIMIT)

    parser.add_argument('--variable-coverage', '-V', action='store_true',
                        dest='variable_coverage', default=False,
                        help='Only trim low-abundance k-mers from sequences '
                        'that have high coverage.')
    parser.add_argument('--tempdir', '-T', type=str, dest='tempdir',
                        default='./')

    parser.add_argument('input_filenames', nargs='+')

    return parser


def main():
    parser = get_parser()
    args = parser.parse_args()

    ###

    if len(set(args.input_filenames)) != len(args.input_filenames):
        print >>sys.stderr, \
            "Error: Cannot input the same filename multiple times."
        sys.exit(1)

    ###

    K = args.ksize
    HT_SIZE = args.min_hashsize
    N_HT = args.n_hashes

    CUTOFF = args.abund_cutoff
    NORMALIZE_LIMIT = args.normalize_to

    print 'making hashtable'
    ht = khmer.new_counting_hash(K, HT_SIZE, N_HT)

    tempdir = tempfile.mkdtemp('khmer', 'tmp', args.tempdir)
    print 'created temporary directory %s; use -T to change location' % tempdir

    # ### FIRST PASS ###

    save_pass2_total = 0

    read_bp = 0
    read_reads = 0
    wrote_bp = 0
    wrote_reads = 0
    trimmed_reads = 0

    pass2list = []
    for filename in args.input_filenames:
        pass2filename = os.path.basename(filename) + '.pass2'
        pass2filename = os.path.join(tempdir, pass2filename)
        trimfilename = os.path.basename(filename) + '.abundtrim'

        pass2list.append((filename, pass2filename, trimfilename))

        screed_iter = screed.open(filename)
        pass2fp = open(pass2filename, 'w')
        trimfp = open(trimfilename, 'w')

        save_pass2 = 0
        for n, is_pair, read1, read2 in broken_paired_reader(screed_iter):
            if n % 10000 == 0:
                print '...', n, filename, save_pass2, read_reads, read_bp, \
                    wrote_reads, wrote_bp

            # we want to track paired reads here, to make sure that pairs
            # are not split between first pass and second pass.

            if is_pair:
                read_reads += 2
                read_bp += len(read1.sequence) + len(read2.sequence)

                seq1 = read1.sequence.replace('N', 'A')
                seq2 = read2.sequence.replace('N', 'A')

                med1, _, _ = ht.get_median_count(seq1)
                med2, _, _ = ht.get_median_count(seq2)

                if med1 < NORMALIZE_LIMIT or med2 < NORMALIZE_LIMIT:
                    ht.consume(seq1)
                    ht.consume(seq2)
                    write_record_pair(read1, read2, pass2fp)
                    save_pass2 += 2
                else:
                    _, trim_at1 = ht.trim_on_abundance(seq1, CUTOFF)
                    _, trim_at2 = ht.trim_on_abundance(seq2, CUTOFF)

                    if trim_at1 >= K:
                        read1 = trim_record(read1, trim_at1)

                    if trim_at2 >= K:
                        read2 = trim_record(read2, trim_at2)

                    if trim_at1 != len(seq1):
                        trimmed_reads += 1
                    if trim_at2 != len(seq2):
                        trimmed_reads += 1

                    write_record_pair(read1, read2, trimfp)
                    wrote_reads += 2
                    wrote_bp += trim_at1 + trim_at2
            else:
                read_reads += 1
                read_bp += len(read1.sequence)

                seq = read1.sequence.replace('N', 'A')
                med, _, _ = ht.get_median_count(seq)

                # has this portion of the graph saturated? if not,
                # consume & save => pass2.
                if med < NORMALIZE_LIMIT:
                    ht.consume(seq)
                    write_record(read1, pass2fp)
                    save_pass2 += 1
                else:                       # trim!!
                    _, trim_at = ht.trim_on_abundance(seq, CUTOFF)
                    if trim_at >= K:
                        new_read = trim_record(read1, trim_at)
                        write_record(new_read, trimfp)

                        wrote_reads += 1
                        wrote_bp += trim_at

                        if trim_at != len(read1.sequence):
                            trimmed_reads += 1

        pass2fp.close()
        trimfp.close()

        print '%s: kept aside %d of %d from first pass, in %s' % \
              (filename, save_pass2, n, filename)
        save_pass2_total += save_pass2

    # ### SECOND PASS. ###

    skipped_n = 0
    skipped_bp = 0
    for orig_filename, pass2filename, trimfilename in pass2list:
        print 'second pass: looking at sequences kept aside in %s' % \
              pass2filename

        # note that for this second pass, we don't care about paired
        # reads - they will be output in the same order they're read in,
        # so pairs will stay together if not orphaned.  This is in contrast
        # to the first loop.

        trimfp = open(trimfilename, 'a')
        for n, read in enumerate(screed.open(pass2filename)):
            if n % 10000 == 0:
                print '... x 2', n, pass2filename, read_reads, read_bp, \
                    wrote_reads, wrote_bp

            seq = read.sequence.replace('N', 'A')
            med, _, _ = ht.get_median_count(seq)

            # do we retain low-abundance components unchanged?
            if med < NORMALIZE_LIMIT and args.variable_coverage:
                write_record(read, trimfp)

                wrote_reads += 1
                wrote_bp += len(read.sequence)
                skipped_n += 1
                skipped_bp += len(read.sequence)

            # otherwise, examine/trim/truncate.
            else:    # med >= NORMALIZE LIMIT or not args.variable_coverage
                _, trim_at = ht.trim_on_abundance(seq, CUTOFF)
                if trim_at >= K:
                    new_read = trim_record(read, trim_at)
                    write_record(new_read, trimfp)

                    wrote_reads += 1
                    wrote_bp += trim_at

                    if trim_at != len(read.sequence):
                        trimmed_reads += 1

        print 'removing %s' % pass2filename
        os.unlink(pass2filename)

    print 'removing temp directory & contents (%s)' % tempdir
    shutil.rmtree(tempdir)

    print 'read %d reads, %d bp' % (read_reads, read_bp,)
    print 'wrote %d reads, %d bp' % (wrote_reads, wrote_bp,)
    print 'removed %d reads and trimmed %d reads' % (read_reads - wrote_reads,
                                                     trimmed_reads,)
    print 'looked at %d reads twice' % (save_pass2_total,)
    print 'trimmed or removed %.2f%% of bases (%d total)' % \
        ((1 - (wrote_bp / float(read_bp))) * 100., read_bp - wrote_bp)

    if args.variable_coverage:
        print 'skipped %d reads/%d bases because of low coverage' % \
              (skipped_n, skipped_bp)

    fp_rate = khmer.calc_expected_collisions(ht)
    print >>sys.stderr, \
        'fp rate estimated to be {fpr:1.3f}'.format(fpr=fp_rate)

    if fp_rate > MAX_FALSE_POSITIVE_RATE:
        print >> sys.stderr, "**"
        print >> sys.stderr, ("** ERROR: the k-mer counting table is too small"
                              " for this data set. Increase tablesize/# "
                              "tables.")
        print >> sys.stderr, "**"
        print >> sys.stderr, "** Do not use these results!!"
        sys.exit(1)

    print 'output in *.abundtrim'


if __name__ == '__main__':
    main()
