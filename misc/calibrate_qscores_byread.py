#!/usr/bin/env python3
import argparse
import os

if True:
    #  Protect in block to prevent autopep8 refactoring
    import matplotlib
    matplotlib.use('Agg')

from Bio import SeqIO
import matplotlib.pyplot as plt
import numpy as np
import statsmodels.api as sm

from taiyaki import fileio


parser = argparse.ArgumentParser(
    description='Calculate parameters to correct qscores as predictor of ' +
    'per-read error rate',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter)

parser.add_argument("--alignment_summary", default=None,
                    help="Input: tsv file containing alignment summary")

parser.add_argument("--coverage_threshold", default=0.8, type=float,
                    help="Disregard reads with coverage less than this")

parser.add_argument("--max_alignment_score", default=40.0, type=float,
                    help="Upper limit on score calculated from alignment")

parser.add_argument("--min_fastqscore", default=7.0, type=float,
                    help="Lower limit on score calculated from fastq")

parser.add_argument("--fastq", default=None,
                    help="Input: fastq file")

parser.add_argument("--input_directory", default=None,
                    help="Input directory containing fastq files and " +
                    "alignment_summary.txt (use either this arg or --fastq")

parser.add_argument('--maxreads', default=None, type=int,
                    help="Max reads to process (default to no max)")

parser.add_argument("--plot_title", default=None,
                    help="Add this title to plot")

parser.add_argument("--plot_filename", default='qscore_calibration.png',
                    help="Output: file name for plot.")


def fastq_file_qscore(qvector):
    """Work out an 'average' q score from an array of q scores in a fastq.

    Notes:
        The average q-score is calculated in probabilty space, so is an estimate
    of the proportion of errors in the read

    Args:
        qvector (:class:`ndarray`): numpy vector of q scores from fastq

    Returns:
        float : single qscore (float) for entire basecall.
    """
    # Error probability at each location
    p = np.power(10.0, -qvector.astype(np.float64) / 10.0)
    # Whole-file mean error rate
    e = np.mean(p)
    return -10.0 * np.log10(e)


def read_fastqs(fastqlist, maxreads=None, reads_per_dot=100):
    """Read fastq files and calculate length and mean q score for each

    Args:
        fastqlist (array of str): list of fastq files
        maxreads (int, optional): max number of files to read, default `None`
            is no maximum.
        reads_per_dot (int, optional): Print a dot for every `reads_per_dot`
            files.

    Returns:
        tuple of :class:`ndarray` and :class:`ndarray` and :class:`ndarray`:
            First element contains read_ids, the second is the corresponding
            mean q-score (NaN if no data was found), and the length of each
            base call.
    """
    read_id_list = []
    mean_qscore_list = []
    length_list = []
    print("Printing one dot for every {} reads.".format(reads_per_dot))
    for fastqfile in fastqlist:
        for record in SeqIO.parse(fastqfile, "fastq"):
            read_id_list.append(record.id)
            scores = np.array(
                record.letter_annotations["phred_quality"])
            length_list.append(len(scores))
            if len(scores) > 0:
                mean_qscore_list.append(fastq_file_qscore(scores))
            else:
                mean_qscore_list.append(None)
            if (len(read_id_list) + 1) % reads_per_dot == 0:
                print(".", end="")
            if maxreads is not None:
                if len(read_id_list) >= maxreads:
                    break
        if maxreads is not None:
            if len(read_id_list) >= maxreads:
                break
    print("")
    return (np.array(read_id_list),
            np.array(mean_qscore_list),
            np.array(length_list))


def get_alignment_data(alignment_file):
    """Read alignment summary generated by Guppy or Taiyaki, getting accuracy
    and length of aligned part of read for each read ID

    Note:
        The resulting table may have more than one entry for each read id
        because there may be more than one possible alignment

    Args:
        alignment_file (str): file path pointing to either Taiyaki (.samacc) or
            Guppy (.txt) alignment summary.

    Returns:
        tuple of :class:`ndarray` and :class:`ndarray` and :class:`ndarray`:
            First element of tuple contains the read ID of the reads analysed,
            the second element is the corresponding accuracy of each read, and
            the third element is the alignment length (-1 means unaligned).
    """
    # Delimiter None accepts space or tab - samaccs are space-separated.
    t = fileio.readtsv(alignment_file, delimiter=None)

    try:
        # Try to read the file as a Guppy alignment summary file
        read_ids = t['read_id']
        accuracies = t['alignment_accuracy']
        alignment_lens = (t['alignment_strand_end']
                          - t['alignment_strand_start'])
        print("Interpreted alignment file as Guppy output")
        accuracies[accuracies < 0] = np.nan
        return read_ids, accuracies, alignment_lens
    except ValueError:
        # Thrown if the required fields are not present in the file
        pass

    try:
        # Try to read the file as a Taiyaki alignment summary
        read_ids = t['query']
        accuracies = t['accuracy']
        # Query length in alignment not available directly in taiyaki summary
        alignment_lens = (t['reference_end']
                          - t['reference_start']
                          + t['insertion']
                          - t['deletion'])
        print("Interpreted alignment file as Taiyaki output")
        return read_ids, accuracies, alignment_lens
    except ValueError:
        pass

    columnlist = list(t.dtype.fields.keys())
    raise Exception("Alignment summary file must contain either columns " +
                    "(read_ids, alignment accuracy, alignment_strand_end, " +
                    "alignment_strand_start) or " +
                    "(id, accuracy, reference_end, reference_start, " +
                    "insertion, deletion  )" +
                    ". Columns are {}".format(columnlist))


def merge_align_fastq_data(fastq_ids,
                           alignment_ids,
                           alignment_accuracies,
                           alignment_lens):
    """Get an alignment accuracy and length of alignment in basecall
    for each id in the fastq data.

    If the alignment has more than one entry for a particular id, then
    choose the most accurate.

    Args:
        fastq_ids (:class:`ndarray`): read_ids taken from the original base call
            files.  Should be unique
        alignment_ids (:class:`ndarray`): read_ids from generated alignments.
            May be duplicated.
        alignment_accuracies (:class:`ndarray`): Accuracy of each alignment.
            Size of array equal to `alignment_ids`.
        alignment_lens (:class:`ndarray`):  Alignment length for each alignment/
            Size of array equal to `alignment_ids`.

    Returns:
        tuple of :class:`ndarray` and :class:`ndarray`:
            Size of output arrays is equal to the size of `fastq_id` and the
            elements correspond to elements of `fastq_id`.
            First element contains the (best) aligment accuracy for each read.
            Second element is the alignment length.

            Where an alignment is not found for an element of `fastq_id`, the
            accuracy is set of Nan and the length is -1.
    """
    n_fastqs = len(fastq_ids)
    fastq_accuracies = np.full(n_fastqs, np.nan)
    fastq_alignment_lens = np.full(n_fastqs, -1)
    read_not_found = 0
    more_than_one_alignment = 0
    for nread, fastq_id in enumerate(fastq_ids):
        accuracies = alignment_accuracies[alignment_ids == fastq_id]
        lens = alignment_lens[alignment_ids == fastq_id]
        if len(accuracies) == 0:
            read_not_found += 1
        elif len(accuracies) == 1:
            fastq_accuracies[nread] = accuracies[0]
            fastq_alignment_lens[nread] = lens[0]
        else:
            more_than_one_alignment += 1
            loc = np.argmax(accuracies)
            fastq_accuracies[nread] = accuracies[loc]
            fastq_alignment_lens[nread] = lens[loc]
    print("\n{} reads read from fastq.".format(n_fastqs))
    print("    {} not found in alignment summary.".format(read_not_found))
    print("    {} with more than one alignment.\n".format(
        more_than_one_alignment))
    return fastq_accuracies, fastq_alignment_lens


def calculate_regression(mean_qscores, calc_qscores):
    """Regress mean fastq qscores against alignment-derived ones.
    Uses Huber regression as in OFAN script

    Args:
        mean_qscores (:class:`ndarray`): mean q-score from base calls
        calc_qscores (:class:`ndarray`): empirical q-scores calculated from
            alignments

    Returns:
        tuple of float and float:  The intercept and slope of the fitted linear
            regression.
    """
    X = sm.add_constant(mean_qscores)

    model = sm.RLM(calc_qscores, X, M=sm.robust.norms.HuberT())

    line = model.fit()
    c, m = line.params

    return c, m


def single_read_accuracy_scatter(accuracies, meanqs, max_alignment_score):
    """Do regression and plot data for single read accuracy vs mean qgenfromtxt

    Empirical q-scores for each read are calculated from its accuracy and a
    (robust) linear regression fitted between with the mena q-score for the
    read as a predictor.

             qscore(accuracy) ~ m * meanq + c

    Args:
        accuracies (:class:`ndarray`): accuracies, as proportions
        meanqs (:class:`ndarray`): mean q-scores
        max_alignment_score (float): clamp empirical q-scores, derived from
            accuracies, greater than this value.

    Returns:
        tuple of float and float: The intercept and slope of the fitted linear
            regression.
    """
    y = -10.0 * np.log10(1.0 - accuracies)
    y[y > max_alignment_score] = max_alignment_score
    x = meanqs

    plt.scatter(x, y, s=2)
    # m_OLS,c_OLS, r_value, p_value, mstd_OLS = linregress(x,y)
    c, m = calculate_regression(x, y)

    xx = np.array([np.min(x), np.max(x)])
    yy = c + m * xx
    label = 'slope={:3.2f} intercept={:3.2f}'.format(m, c)
    plt.plot(xx, yy, color='gray', label=label)
    plt.plot(xx, xx, color='gray', linestyle='dotted', label='y=x')
    plt.legend(loc='upper left', framealpha=0.1)
    plt.xlabel('Fastq q score')
    plt.ylabel('Alignment accuracy score')
    plt.grid()
    return m, c


def filter_data(accuracies, fastqscores, fastq_lens, alignment_lens,
                min_coverage, min_fastqscore):
    """Remove null accuracies, low coveage and poor quality

        Filter reads where:
            accuracy is NaN (unaligned)
            coverage < `min_coverage`
            fastqscore < `min_fastqscore`

    Note:
        Measure of coverage used (as in ONT calibration script) is
        (aligned length in basecall) / (total basecall length)

    Args:
        accuracies (:class:`ndarray`): accuracy of each alignment, NaN if
            unaligned.
        qscores (:class:`ndarray`): mean quality score for read.
        fastq_lens (:class:`ndarray`): length of each base call.
        alignment_lens (:class:`ndarray`): length of each alignment.
        min_coverage (float): minimum coverage fraction to include.
        min_fastqscore (float): minimum fastq score to include.

    Returns:
        tuple of :class:`ndarray` and :class:`ndarray`:
            `accuracies` and `fastqscores` filtered.
    """
    # Make filter to remove unaligned reads
    f = ~np.isnan(accuracies)

    coverage_fraction = (alignment_lens.astype(np.float64) /
                         fastq_lens.astype(np.float64))
    # Also remove coverage less than threshold (values of -1 in alignment_len
    # used to indicate null also filtered out by this step
    g = (coverage_fraction > min_coverage)

    h = (fastqscores >= min_fastqscore)

    print("Total number of reads = ", len(accuracies))
    print("    After removing those not aligned:", len(accuracies[f]))
    print("    After also removing coverage < {:3.2f}: {}".format(
        min_coverage, len(accuracies[f & g])))
    print("    After also removing fastq score < {:3.1f}: {}".format(
        min_fastqscore, len(accuracies[f & g & h])))

    return accuracies[f & g & h], fastqscores[f & g & h]


if __name__ == "__main__":
    print("Calculating shift and scale parameters to calibrate per-read")
    print("accuracy estimates from q scores.")
    args = parser.parse_args()
    fastqlist = None
    if args.input_directory is not None:
        fastqlist = [fi for fi in os.listdir(args.input_directory)
                     if fi.endswith('.fastq')]
        fastqlist = [os.path.join(args.input_directory, fi)
                     for fi in fastqlist]
        if len(fastqlist) == 0:
            errstr = "No fastq files found in {}".format(args.input_directory)
            raise Exception(errstr)
        else:
            print("Getting q scores for {} fastq files from {}".format(
                len(fastqlist), args.input_directory))
        alignment_summary_file = os.path.join(args.input_directory,
                                              'alignment_summary.txt')
    if args.fastq is not None:
        fastqlist = [args.fastq]
        if fastqlist is not None:
            print("Command-line argument fastq overrides directory list")
        print("Calculating average q scores for {}".format(args.fastq))

    if args.alignment_summary is not None:
        # args.alignment summary overrides the one in the directory.
        print("Using alignment summary file at ", args.alignment_summary)
        alignment_summary_file = args.alignment_summary

    if fastqlist is None:
        raise Exception("You must supply a directory containing " +
                        "fastqs or the path to a fastq file")

    fastq_ids, fastq_meanqs, fastq_lens = read_fastqs(fastqlist, args.maxreads)

    align_ids, align_accuracies, align_lens = get_alignment_data(
        alignment_summary_file)

    fastq_accuracies, fastq_align_lens = merge_align_fastq_data(
        fastq_ids, align_ids, align_accuracies, align_lens)

    fastq_accuracies, fastq_meanqs = filter_data(
        fastq_accuracies, fastq_meanqs, fastq_lens, fastq_align_lens,
        args.coverage_threshold, args.min_fastqscore)

    slope, intercept = single_read_accuracy_scatter(
        fastq_accuracies, fastq_meanqs, args.max_alignment_score)

    print("\n\nBest-fit:", args.plot_title)
    print("Best-fit slope (qscore_scale) = {:3.4f}".format(slope))
    print("Best-fit shift (qscore_shift) = {:3.4f}".format(intercept))

    if args.plot_title is not None:
        plt.title(args.plot_title)

    print("\nSaving plot to {}".format(args.plot_filename))
    plt.savefig(args.plot_filename)
    plt.close()
