from past.builtins import basestring
from functools import reduce
import argparse
import logging, coloredlogs
import common
import os
import re
import six
import sys
import math
import random
import json
import types
import collections
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import scipy
import scipy.misc
import scipy.stats

logger = logging.getLogger(__name__)
coloredlogs.install(level=logging.DEBUG)


Combined = collections.namedtuple('Combined', ['poly', 'expp', 'exp_cnt', 'obs_cnt', 'zscore'])
CombinedIdx = collections.namedtuple('CombinedIdx', ['poly', 'expp', 'exp_cnt', 'obs_cnt', 'zscore', 'idx'])
ValueIdx = collections.namedtuple('ValueIdx', ['value', 'idx'])


def bar_chart(sources=None, values=None, res=None, error=None, xlabel=None, title=None):
    if res is not None:
        sources = [x[0] for x in res]
        values = [x[1] for x in res]

    plt.rcdefaults()
    y_pos = np.arange(len(sources))
    plt.barh(y_pos, values, align='center', xerr=error, alpha=0.4)
    plt.yticks(y_pos, sources)
    plt.xlabel(xlabel)
    plt.title(title)
    plt.show()


class HWAnalysis(object):
    """
    Analysis of all deg poly
    """
    def __init__(self, *args, **kwargs):
        self.term_map = []
        self.term_eval = None
        self.ref_term_eval = None

        self.blocklen = None
        self.deg = 3
        self.top_k = None
        self.comb_random = None
        self.top_comb = None
        self.zscore_thresh = 1.96
        self.combine_all_deg = False
        self.do_ref = False
        self.no_comb_xor = False
        self.no_comb_and = False

        self.total_hws = []
        self.ref_total_hws = []
        self.total_n = 0

        self.all_deg_compute = True
        self.input_poly = []
        self.input_poly_exp = []
        self.input_poly_hws = []
        self.input_poly_ref_hws = []

        # Buffers - allocated during computation for fast copy evaluation
        self.comb_res = None
        self.comb_subres = None

    def init(self):
        """
        Initializes state, term_eval engine, input polynomials expected probability.
        :return:
        """
        logger.info('Precomputing term mappings')
        self.term_map = common.build_term_map(self.deg, self.blocklen)
        self.term_eval = common.TermEval(blocklen=self.blocklen, deg=self.deg)
        self.ref_term_eval = common.TermEval(blocklen=self.blocklen, deg=self.deg)
        self.total_hws = [[0] * common.comb(self.blocklen, x, True) for x in range(self.deg + 1)]
        self.ref_total_hws = [[0] * common.comb(self.blocklen, x, True) for x in range(self.deg + 1)]
        self.input_poly_exp = [0] * len(self.input_poly)
        self.input_poly_hws = [0] * len(self.input_poly)
        self.input_poly_ref_hws = [0] * len(self.input_poly)
        self.precompute_input_poly()

    def precompute_input_poly(self):
        """
        Precompute expected values for input polynomials
        :return:
        """
        self.input_poly_exp = []
        for poly in self.input_poly:
            exp_cnt = self.term_eval.expp_poly(poly)
            self.input_poly_exp.append(exp_cnt)

    def proces_chunk(self, bits, ref_bits=None):
        """
        Processes input chunk of bits for analysis.
        :param bits:
        :param ref_bits:
        :return:
        """
        # Compute the basis.
        self.term_eval.load(bits)
        ln = len(bits)
        hws2, hws_input = None, None

        # Evaluate all terms of degrees 1..deg
        if self.all_deg_compute:
            logger.info('Evaluating all terms, bitlen: %d, bytes: %d' % (ln, ln//8))
            hws2 = self.term_eval.eval_all_terms(self.deg)
            logger.info('Done: %s' % [len(x) for x in hws2])

            # Accumulate hws to the results.
            for d in range(1, self.deg+1):
                for i in range(len(self.total_hws[d])):
                    self.total_hws[d][i] += hws2[d][i]

        # Evaluate given polynomials
        if len(self.input_poly) > 0:
            comb_res = self.term_eval.new_buffer()
            comb_subres = self.term_eval.new_buffer()
            hws_input = [0] * len(self.input_poly)
            for idx, poly in enumerate(self.input_poly):
                obs_cnt = self.term_eval.hw(self.term_eval.eval_poly(poly, res=comb_res, subres=comb_subres))
                hws_input[idx] = obs_cnt
                self.input_poly_hws[idx] += obs_cnt

        self.total_n += self.term_eval.cur_evals

        # Reference stream
        ref_hws = self.process_ref(ref_bits, ln)

        # Done.
        self.analyse(num_evals=self.term_eval.cur_evals, hws=hws2, hws_input=hws_input, ref_hws=ref_hws)

    def process_ref(self, ref_bits, ln):
        """
        Process reference data stream
        :return:
        """
        if ref_bits is None:
            return None

        if len(ref_bits) != ln:
            raise ValueError('Reference data stream has a different size')

        logger.info('Evaluating ref data stream')
        if self.all_deg_compute:
            self.ref_term_eval.load(ref_bits)
            ref_hws = self.ref_term_eval.eval_all_terms(self.deg)
            for d in range(1, self.deg+1):
                for i in range(len(self.ref_total_hws[d])):
                    self.ref_total_hws[d][i] += ref_hws[d][i]
            return ref_hws

        else:
            return None

    def finished(self):
        """
        All data read - final analysis.
        :return:
        """
        self.analyse(self.total_hws, self.total_n)

    def analyse_input(self, num_evals, hws_input=None):
        """
        Analyses input polynomials result on the data
        :param num_evals:
        :param hws_input:
        :return:
        """
        if hws_input is None:
            return

        results = [None] * len(self.input_poly)
        for idx, poly in enumerate(self.input_poly):
            expp = self.input_poly_exp[idx]
            exp_cnt = num_evals * expp
            obs_cnt = hws_input[idx]
            zscore = common.zscore(obs_cnt, exp_cnt, num_evals)
            results[idx] = CombinedIdx(None, expp, exp_cnt, obs_cnt, zscore, idx)

        # Sort by the zscore
        results.sort(key=lambda x: abs(x.zscore), reverse=True)

        for res in results:
            fail = 'x' if abs(res.zscore) > self.zscore_thresh else ' '
            print(' - zscore[idx%02d]: %+05.5f, observed: %08d, expected: %08d %s idx: %6d, poly: %s'
                  % (res.idx, res.zscore, res.obs_cnt, res.exp_cnt, fail, res.idx, self.input_poly[res.idx]))

    def analyse(self, num_evals, hws=None, hws_input=None, ref_hws=None):
        """
        Analyse hamming weights
        :param num_evals:
        :param hws: hamming weights on results for all degrees
        :param hws_input: hamming weights on results for input polynomials
        :param ref_hws: reference hamming weights
        :return:
        """

        # Input polynomials
        self.analyse_input(num_evals=num_evals, hws_input=hws_input)

        # All degrees polynomials + combinations
        if not self.all_deg_compute:
            return

        probab = [self.term_eval.expp_term_deg(deg) for deg in range(0, self.deg + 1)]
        exp_count = [num_evals * x for x in probab]
        print(probab)
        print(exp_count)

        top_terms = []
        zscores = [[0] * len(x) for x in hws]
        zscores_ref = [[0] * len(x) for x in hws]
        for deg in range(1, self.deg+1):
            # Compute (zscore, idx)
            # If reference stream is used, compute diff zscore.
            if ref_hws is not None:
                zscores_ref[deg] = [common.zscore(x, exp_count[deg], num_evals) for x in ref_hws[deg]]
                zscores[deg] = [((common.zscore(x, exp_count[deg], num_evals)), idx, x) for idx, x in enumerate(hws[deg])] #- zscores_ref[deg][idx]
                zscores_ref[deg].sort(key=lambda x: abs(x), reverse=True)
            else:
                zscores[deg] = [(common.zscore(x, exp_count[deg], num_evals), idx, x) for idx, x in enumerate(hws[deg])]
            zscores[deg].sort(key=lambda x: abs(x[0]), reverse=True)

            # Selecting TOP k polynomials
            for idx, x in enumerate(zscores[deg][0:15]):
                fail = 'x' if abs(x[0]) > self.zscore_thresh else ' '
                print(' - zscore[deg=%d]: %+05.5f, %+05.5f, observed: %08d, expected: %08d %s idx: %6d, term: %s'
                      % (deg, x[0], zscores_ref[deg][idx]-x[0], x[2], exp_count[deg], fail, x[1], self.term_map[deg][x[1]]))

            # Take top X best polynomials
            if self.top_k is None:
                continue

            if self.combine_all_deg or deg == self.deg:
                top_terms += [self.term_map[deg][x[1]] for x in zscores[deg][0: (None if self.top_k < 0 else self.top_k)]]

                if self.comb_random > 0:
                    random_subset = random.sample(zscores[deg], self.comb_random)
                    top_terms += [self.term_map[deg][x[1]] for x in random_subset]

            mean_zscore = sum([x[0] for x in zscores[deg]])/float(len(zscores[deg]))
            fails = sum([1 for x in zscores[deg] if abs(x[0]) > self.zscore_thresh])
            fails_fraction = float(fails)/len(zscores[deg])
            # total_fails.append(fails_fraction)
            print('Mean zscore[deg=%d]: %s' % (deg, mean_zscore))
            print('Num of fails[deg=%d]: %s = %02f.5%%' % (deg, fails, 100.0*fails_fraction))

        if self.top_k is None:
            return

        # Combine & store the results - XOR
        top_res = []
        logger.info('Combining %d terms in %d degree...' % (len(top_terms), self.top_comb))

        self.comb_res = self.term_eval.new_buffer()
        self.comb_subres = self.term_eval.new_buffer()
        for top_comb_cur in range(1, self.top_comb + 1):

            # Combine * store results - XOR
            if not self.no_comb_xor:
                self.comb_xor(top_comb_cur=top_comb_cur, top_terms=top_terms, top_res=top_res, num_evals=num_evals,
                              ref_hws=ref_hws)

            # Combine & store results - AND
            if not self.no_comb_and:
                self.comb_and(top_comb_cur=top_comb_cur, top_terms=top_terms, top_res=top_res, num_evals=num_evals,
                              ref_hws=ref_hws)

        logger.info('Evaluating')
        top_res.sort(key=lambda x: abs(x.zscore), reverse=True)
        for i in range(min(len(top_res), 30)):
            comb = top_res[i]
            print(' - best poly zscore %9.5f, expp: %.4f, exp: %4d, obs: %s, diff: %f %%, poly: %s'
                  % (comb.zscore, comb.expp, comb.exp_cnt, comb.obs_cnt,
                     100.0 * (comb.exp_cnt - comb.obs_cnt) / comb.exp_cnt, sorted(comb.poly)))

    def comb_xor(self, top_comb_cur, top_terms, top_res, num_evals, ref_hws=None):
        """
        Combines top terms with XOR operation
        :param top_comb_cur: current degree of the combination
        :param top_terms: top terms buffer to choose terms out of
        :param top_res: top results accumulator to put
        :param num_evals: number of evaluations in this round - zscore computation
        :param ref_hws: reference results
        :return:
        """
        for idx, places in enumerate(common.term_generator(top_comb_cur, len(top_terms) - 1)):
            poly = [top_terms[x] for x in places]
            expp = self.term_eval.expp_poly(poly)
            exp_cnt = num_evals * expp
            if exp_cnt == 0:
                continue

            obs_cnt = self.term_eval.hw(self.term_eval.eval_poly(poly, res=self.comb_res, subres=self.comb_subres))
            zscore = common.zscore(obs_cnt, exp_cnt, num_evals)

            comb = None
            if ref_hws is None:
                comb = Combined(poly, expp, exp_cnt, obs_cnt, zscore)
            else:
                ref_obs_cnt = self.ref_term_eval.hw(
                    self.ref_term_eval.eval_poly(poly, res=self.comb_res, subres=self.comb_subres))
                zscore_ref = common.zscore(ref_obs_cnt, exp_cnt, num_evals)
                comb = Combined(poly, expp, exp_cnt, obs_cnt, zscore - zscore_ref)
            top_res.append(comb)

    def comb_and(self, top_comb_cur, top_terms, top_res, num_evals, ref_hws=None):
        """
        Combines top terms with AND operation
        :param top_comb_cur: current degree of the combination
        :param top_terms: top terms buffer to choose terms out of
        :param top_res: top results accumulator to put
        :param num_evals: number of evaluations in this round - zscore computation
        :param ref_hws: reference results
        :return:
        """
        for idx, places in enumerate(common.term_generator(top_comb_cur, len(top_terms) - 1)):
            poly = [reduce(lambda x, y: x + y, [top_terms[x] for x in places])]
            expp = self.term_eval.expp_poly(poly)
            exp_cnt = self.term_eval.cur_evals * expp
            if exp_cnt == 0:
                continue

            obs_cnt = self.term_eval.hw(self.term_eval.eval_poly(poly, res=self.comb_res, subres=self.comb_subres))
            zscore = common.zscore(obs_cnt, exp_cnt, num_evals)

            comb = None
            if ref_hws is None:
                comb = Combined(poly, expp, exp_cnt, obs_cnt, zscore)
            else:
                ref_obs_cnt = self.ref_term_eval.hw(
                    self.ref_term_eval.eval_poly(poly, res=self.comb_res, subres=self.comb_subres))
                zscore_ref = common.zscore(ref_obs_cnt, exp_cnt, num_evals)
                comb = Combined(poly, expp, exp_cnt, obs_cnt, zscore - zscore_ref)
            top_res.append(comb)


# Main - argument parsing + processing
class App(object):
    def __init__(self, *args, **kwargs):
        self.args = None
        self.tester = None
        self.blocklen = None
        self.term_map = []
        self.input_poly = []

    def defset(self, val, default=None):
        return val if val is not None else default

    def independence_test(self, term_eval, ddeg=3, vvar=10):
        """
        Experimental verification of term independence.
        :param term_eval:
        :param ddeg:
        :param vvar:
        :return:
        """
        tterms = common.comb(vvar, ddeg)
        print('Independence test C(%d, %d) = %s' % (vvar, ddeg, tterms))
        ones = [0] * common.comb(vvar, ddeg, True)

        for val in common.pos_generator(dim=vvar, maxelem=1):
            for idx, term in enumerate(common.term_generator(ddeg, vvar - 1)):
                ones[idx] += term_eval.eval_term_raw_single(term, val)
        print('Done')
        print(ones)
        # TODO: test slight bias - in the allowed boundaries...

    def get_testing_polynomials(self):
        return [
            [[0]],
            [[0, 1]],
            [[0, 1, 2]],
            [[0, 1, 2], [0]],
            [[0, 1, 2], [0, 1]],
            [[0, 1, 2], [3]],
            [[0, 1, 2], [2, 3, 4]],
            [[0, 1, 2], [1, 2, 3]],
            [[0, 1, 2], [3, 4, 5]],
            [[5, 6, 7], [8, 9, 10]],
            [[5, 6, 7], [7, 8, 9]],
            [[1, 2], [2, 3], [1, 3]],
            [[0, 1, 2], [2, 3, 4], [5, 6, 7]],
            [[0, 1, 2], [2, 3, 4], [1, 2, 3]],
        ]

    def get_multiplier(self, char, is_ib=False):
        """
        Returns the multiplier factor of the multiplier character. if ib is enabled, powers of
        1024 are returned, otherwise powers of 1000.

        :param char:
        :param is_ib:
        :return:
        """
        if char is None or len(char) == 0:
            return 1

        char = char[:1].lower()
        if char == 'k':
            return 1024 if is_ib else 1000
        elif char == 'm':
            return 1024 * 1024 if is_ib else 1000 * 1000
        elif char == 'g':
            return 1024 * 1024 * 1024 if is_ib else 1000 * 1000 * 1000
        elif char == 't':
            return 1024 * 1024 * 1024 * 1024 if is_ib else 1000 * 1000 * 1000 * 1000
        else:
            raise ValueError('Unknown multiplier %s' % char)

    def process_size(self, size_param):
        """
        Processes size parameter and evaluates the multipliers (e.g., 3M).
        :param size_param:
        :return:
        """
        if size_param is None:
            return None

        if isinstance(size_param, (int, long)):
            return size_param

        if not isinstance(size_param, basestring):
            raise ValueError('Unknown type of the input parameter')

        if len(size_param) == 0:
            return None

        if size_param.isdigit():
            return int(size_param)

        matches = re.match('^([0-9a-fA-F]+(.[0-9]+)?)([kKmMgGtT]([iI])?)?$', size_param)
        if matches is None:
            raise ValueError('Unknown size specifier')

        is_ib = matches.group(4) is not None
        mult_char = matches.group(3)
        multiplier = self.get_multiplier(mult_char, is_ib)
        return int(float(matches.group(1)) * multiplier)

    # noinspection PyMethodMayBeStatic
    def _fix_poly(self, poly):
        """
        Checks if the input polynomial is a valid polynomial
        :param poly:
        :return:
        """
        if not isinstance(poly, types.ListType):
            raise ValueError('Polynomial is not valid (list expected) %s' % poly)

        if len(poly) == 0:
            raise ValueError('Empty polynomial not allowed')

        first_elem = poly[0]
        if not isinstance(first_elem, types.ListType):
            poly = [poly]

        for idxt, term in enumerate(poly):
            if not isinstance(term, types.ListType):
                raise ValueError('Term %s in the polynomial %s is not valid (list expected)' % (term, poly))
            for idxv, var in enumerate(term):
                if not isinstance(var, (types.IntType, types.LongType)):
                    raise ValueError('Variable %s not valid in the polynomial %s (number expected)' % (var, poly))
                if var >= self.blocklen:
                    if self.args.poly_ignore:
                        return None
                    elif self.args.poly_mod:
                        poly[idxt][idxv] = var % self.blocklen
                    else:
                        raise ValueError('Variable %s not valid in the polynomial %s (blocklen is %d)'
                                         % (var, poly, self.blocklen))

        return poly

    def load_input_poly(self):
        """
        Loads input polynomials.
        :return:
        """
        for poly in self.args.polynomials:
            poly_js = self._fix_poly(json.loads(poly))
            self.input_poly.append(poly_js)

        for poly_file in self.args.poly_file:
            with open(poly_file, 'r') as fh:
                for line in fh:
                    line = line.strip()
                    if len(line) == 0:
                        continue
                    if line.startswith('#'):
                        continue
                    if line.startswith('//'):
                        continue
                    print(line)
                    poly_js = self._fix_poly(json.loads(line))
                    if poly_js is None:
                        continue
                    self.input_poly.append(poly_js)

                logger.debug('Poly file %s loaded' % poly_file)

        logger.debug('Input polynomials length: %s' % len(self.input_poly))

    def work(self):
        self.blocklen = int(self.defset(self.args.blocklen, 128))
        deg = int(self.defset(self.args.degree, 3))
        tvsize_orig = int(self.defset(self.process_size(self.args.tvsize), 1024*256))
        zscore_thresh = float(self.args.conf)
        rounds = int(self.args.rounds) if self.args.rounds is not None else None
        top_k = int(self.args.topk) if self.args.topk is not None else None
        top_comb = int(self.defset(self.args.combdeg, 2))
        reffile = self.defset(self.args.reffile)
        all_deg = self.args.alldeg

        # Load input polynomials
        self.load_input_poly()

        logger.info('Basic settings, deg: %s, blocklen: %s, TV size: %s, rounds: %s'
                    % (deg, self.blocklen, tvsize_orig, rounds))

        # specific polynomial testing
        logger.info('Initialising')
        poly_test = self.get_testing_polynomials()
        poly_acc = [0] * len(poly_test)

        # test polynomials
        term_eval = common.TermEval(blocklen=self.blocklen, deg=deg)
        for idx, poly in enumerate(poly_test):
            print('Test polynomial: %02d, %s' % (idx, poly))
            expp = term_eval.expp_poly(poly)
            print('  Expected probability: %s' % expp)

        # read file by file
        for file in self.args.files:
            tvsize = tvsize_orig

            if not os.path.exists(file):
                logger.error('File does not exist: %s' % file)

            size = os.path.getsize(file)
            logger.info('Testing file: %s, size: %d kB' % (file, size/1024.0))

            # size smaller than TV? Adapt tv then
            if size < tvsize:
                logger.info('File size is smaller than TV, updating TV to %d' % size)
                tvsize = size

            hwanalysis = HWAnalysis()
            hwanalysis.deg = deg
            hwanalysis.blocklen = self.blocklen
            hwanalysis.top_comb = top_comb
            hwanalysis.comb_random = self.args.comb_random
            hwanalysis.top_k = top_k
            hwanalysis.combine_all_deg = all_deg
            hwanalysis.zscore_thresh = zscore_thresh
            hwanalysis.do_ref = reffile is not None
            hwanalysis.input_poly = self.input_poly
            hwanalysis.no_comb_and = self.args.no_comb_and
            hwanalysis.no_comb_xor = self.args.no_comb_xor

            # compute classical analysis only if there are no input polynomials
            hwanalysis.all_deg_compute = len(self.input_poly) == 0
            logger.info('Initializing test')
            hwanalysis.init()

            total_terms = int(scipy.misc.comb(self.blocklen, deg, True))
            logger.info('BlockLength: %d, deg: %d, terms: %d' % (self.blocklen, deg, total_terms))

            # read the file until there is no data.
            # TODO: sys.stdin
            fref = None
            if reffile is not None:
                fref = open(reffile, 'r')
            with open(file, 'r') as fh:
                data_read = 0
                cur_round = 0

                while data_read < size:
                    if rounds is not None and cur_round > rounds:
                        break

                    data = fh.read(tvsize)
                    bits = common.to_bitarray(data)
                    if len(bits) == 0:
                        logger.info('File read completely')
                        break

                    ref_bits = None
                    if fref is not None:
                        ref_data = fref.read(tvsize)
                        ref_bits = common.to_bitarray(ref_data)

                    logger.info('Pre-computing with TV, deg: %d, blocklen: %04d, tvsize: %08d = %8.2f kB = %8.2f MB, '
                                'round: %d, avail: %d' %
                                (deg, self.blocklen, tvsize, tvsize/1024.0, tvsize/1024.0/1024.0, cur_round, len(bits)))

                    hwanalysis.proces_chunk(bits, ref_bits)
                    cur_round += 1
                pass

            if fref is not None:
                fref.close()
        logger.info('Processing finished')

    def main(self):
        logger.debug('App started')

        parser = argparse.ArgumentParser(description='PolyDist')
        parser.add_argument('-t', '--threads', dest='threads', type=int, default=None,
                            help='Number of threads to use')
        parser.add_argument('--debug', dest='debug', action='store_const', const=True,
                            help='enables debug mode')
        parser.add_argument('--verbose', dest='verbose', action='store_const', const=True,
                            help='enables verbose mode')

        parser.add_argument('--ref', dest='reffile',
                            help='reference file with random data')

        parser.add_argument('--block', dest='blocklen',
                            help='block size in bits')
        parser.add_argument('--degree', dest='degree',
                            help='maximum degree of computation')
        parser.add_argument('--tv', dest='tvsize',
                            help='Size of one test vector, in this interpretation = number of bytes to read from file. '
                                 'Has to be aligned on block size')
        parser.add_argument('-r', '--rounds', dest='rounds',
                            help='Maximal number of rounds')

        parser.add_argument('--top', dest='topk', default=30, type=int,
                            help='top K number of best distinguishers to combine together')

        parser.add_argument('--comb-rand', dest='comb_random', default=0, type=int,
                            help='number of terms to add randomly to the combination set')

        parser.add_argument('--combine-deg', dest='combdeg', default=2, type=int,
                            help='Degree of combination')

        parser.add_argument('--conf', dest='conf', type=float, default=1.96,
                            help='Zscore failing threshold')

        parser.add_argument('--alldeg', dest='alldeg', action='store_const', const=True, default=False,
                            help='Add top K best terms to the combination group also for lower degree, not just top one')

        parser.add_argument('--stdin', dest='stdin', action='store_const', const=True,
                            help='read data from STDIN')

        parser.add_argument('--poly', dest='polynomials', nargs=argparse.ZERO_OR_MORE, default=[],
                            help='input polynomial to evaluate on the input data instead of generated one')

        parser.add_argument('--poly-file', dest='poly_file', nargs=argparse.ZERO_OR_MORE, default=[],
                            help='input file with polynomials to test, one polynomial per line, in json array notation')

        parser.add_argument('--poly-ignore', dest='poly_ignore', action='store_const', const=True, default=False,
                            help='Ignore input polynomial variables out of range')

        parser.add_argument('--poly-mod', dest='poly_mod', action='store_const', const=True, default=False,
                            help='Mod input polynomial variables out of range')

        parser.add_argument('--no-comb-xor', dest='no_comb_xor', action='store_const', const=True, default=False,
                            help='Disables XOR combinations')

        parser.add_argument('--no-comb-and', dest='no_comb_and', action='store_const', const=True, default=False,
                            help='Disables AND combinations')

        parser.add_argument('files', nargs=argparse.ZERO_OR_MORE, default=[],
                            help='files to process')

        self.args = parser.parse_args()
        self.work()


# Launcher
app = None
if __name__ == "__main__":
    app = App()
    app.main()

