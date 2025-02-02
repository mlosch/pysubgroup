"""
Created on 29.04.2016

@author: lemmerfn
"""
import copy
import warnings
from collections import Counter, defaultdict, namedtuple
from heapq import heappop, heappush
from itertools import chain, combinations
from math import factorial

import time
import numpy as np

import pysubgroup as ps

from multiprocessing import Pool


class SubgroupDiscoveryTask:
    """
    Capsulates all parameters required to perform standard subgroup discovery
    """

    def __init__(
        self,
        data,
        target,
        search_space,
        qf,
        result_set_size=10,
        depth=3,
        min_quality=float("-inf"),
        constraints=None,
    ):
        self.data = data
        self.target = target
        self.search_space = search_space
        self.qf = qf
        self.result_set_size = result_set_size
        self.depth = depth
        self.min_quality = min_quality
        if constraints is None:
            constraints = []
        self.constraints = constraints
        self.constraints_monotone = [
            constr for constr in constraints if constr.is_monotone
        ]
        self.constraints_other = [
            constr for constr in constraints if not constr.is_monotone
        ]


def constraints_satisfied(constraints, subgroup, statistics=None, data=None):
    return all(
        constr.is_satisfied(subgroup, statistics, data) for constr in constraints
    )


try:  # pragma: no cover
    from numba import (  # pylint: disable=import-error, import-outside-toplevel
        int32,
        int64,
        njit,
    )

    @njit([(int32[:, :], int64[:])], cache=True)
    def getNewCandidates(candidates, hashes):  # pragma: no cover
        result = []
        for i in range(len(candidates) - 1):
            for j in range(i + 1, len(candidates)):
                if hashes[i] == hashes[j]:
                    if np.all(candidates[i, :-1] == candidates[j, :-1]):
                        result.append((i, j))
        return result

except ImportError:  # pragma: no cover
    pass


class Apriori:
    def __init__(
        self, representation_type=None, combination_name="Conjunction", use_numba=True
    ):
        self.combination_name = combination_name

        if representation_type is None:
            representation_type = ps.BitSetRepresentation
        self.representation_type = representation_type
        self.use_vectorization = True
        self.optimistic_estimate_name = "optimistic_estimate"
        self.next_level = self.get_next_level
        self.compiled_func = None
        if use_numba:  # pragma: no cover
            try:
                import numba  # pylint: disable=unused-import, import-outside-toplevel # noqa: F401, E501

                self.next_level = self.get_next_level_numba
                print("Apriori: Using numba for speedup")
            except ImportError:
                pass

    def get_next_level_candidates(self, task, result, next_level_candidates):
        promising_candidates = []
        optimistic_estimate_function = getattr(task.qf, self.optimistic_estimate_name)
        for sg in next_level_candidates:
            statistics = task.qf.calculate_statistics(sg, task.target, task.data)
            ps.add_if_required(
                result,
                sg,
                task.qf.evaluate(sg, task.target, task.data, statistics),
                task,
                statistics=statistics,
            )
            optimistic_estimate = optimistic_estimate_function(
                sg, task.target, task.data, statistics
            )

            if optimistic_estimate >= ps.minimum_required_quality(
                result, task
            ) and ps.constraints_satisfied(
                task.constraints_monotone, sg, statistics, task.data
            ):
                promising_candidates.append((optimistic_estimate, sg.selectors))
        min_quality = ps.minimum_required_quality(result, task)
        promising_candidates = [
            selectors
            for estimate, selectors in promising_candidates
            if estimate > min_quality
        ]
        return promising_candidates

    def get_next_level_candidates_vectorized(self, task, result, next_level_candidates):
        promising_candidates = []
        statistics = []
        optimistic_estimate_function = getattr(task.qf, self.optimistic_estimate_name)
        next_level_candidates = list(next_level_candidates)
        if len(next_level_candidates) == 0:
            return []
        for sg in next_level_candidates:
            statistics.append(task.qf.calculate_statistics(sg, task.target, task.data))
        tpl_class = statistics[0].__class__
        vec_statistics = tpl_class._make(np.array(tpl) for tpl in zip(*statistics))
        qualities = task.qf.evaluate(
            slice(None), task.target, task.data, vec_statistics
        )
        optimistic_estimates = optimistic_estimate_function(
            None, None, None, vec_statistics
        )

        for sg, quality, stats in zip(next_level_candidates, qualities, statistics):
            ps.add_if_required(result, sg, quality, task, statistics=stats)

        min_quality = ps.minimum_required_quality(result, task)
        for sg, optimistic_estimate in zip(next_level_candidates, optimistic_estimates):
            if optimistic_estimate >= min_quality:
                promising_candidates.append(sg.selectors)
        return promising_candidates

    def get_next_level_numba(self, promising_candidates):  # pragma: no cover
        if not hasattr(self, "compiled_func") or self.compiled_func is None:
            self.compiled_func = getNewCandidates

        all_selectors = Counter(chain.from_iterable(promising_candidates))
        all_selectors_ids = {selector: i for i, selector in enumerate(all_selectors)}
        promising_candidates_selector_ids = [
            tuple(all_selectors_ids[sel] for sel in selectors)
            for selectors in promising_candidates
        ]
        shape1 = len(promising_candidates_selector_ids)
        if shape1 == 0:
            return []
        shape2 = len(promising_candidates_selector_ids[0])
        arr = np.array(promising_candidates_selector_ids, dtype=np.int32).reshape(
            shape1, shape2
        )

        print(len(arr))
        hashes = np.array(
            [hash(tuple(x[:-1])) for x in promising_candidates_selector_ids],
            dtype=np.int64,
        )
        print(len(arr), arr.dtype, hashes.dtype)
        candidates_int = self.compiled_func(arr, hashes)
        return [
            (*promising_candidates[i], promising_candidates[j][-1])
            for i, j in candidates_int
        ]

    def get_next_level(self, promising_candidates):
        by_prefix_dict = defaultdict(list)
        for sg in promising_candidates:
            by_prefix_dict[tuple(sg[:-1])].append(sg[-1])
        return [
            prefix + real_suffix
            for prefix, suffixes in by_prefix_dict.items()
            for real_suffix in combinations(sorted(suffixes), 2)
        ]

    def execute(self, task):
        if not isinstance(
            task.qf, ps.BoundedInterestingnessMeasure
        ):  # pragma: no cover
            warnings.warn(
                "Quality function is unbounded, long runtime expected", RuntimeWarning
            )

        task.qf.calculate_constant_statistics(task.data, task.target)

        with self.representation_type(task.data, task.search_space) as representation:
            combine_selectors = getattr(representation.__class__, self.combination_name)
            result = []
            # init the first level
            next_level_candidates = []
            for sel in task.search_space:
                sg = combine_selectors([sel])
                if ps.constraints_satisfied(
                    task.constraints_monotone, sg, None, task.data
                ):
                    next_level_candidates.append(sg)

            # level-wise search
            depth = 1
            while next_level_candidates:
                # check sgs from the last level
                if self.use_vectorization:
                    promising_candidates = self.get_next_level_candidates_vectorized(
                        task, result, next_level_candidates
                    )
                else:
                    promising_candidates = self.get_next_level_candidates(
                        task, result, next_level_candidates
                    )
                if len(promising_candidates) == 0:
                    break

                if depth == task.depth:
                    break

                next_level_candidates_no_pruning = self.next_level(promising_candidates)

                # select those selectors and build a subgroup from them
                #   for which all subsets of length depth (=candidate length -1)
                #   are in the set of promising candidates
                curr_depth = depth  # WARNING: need copy of depth for lazy eval
                set_promising_candidates = set(tuple(p) for p in promising_candidates)
                next_level_candidates = (
                    combine_selectors(selectors)
                    for selectors in next_level_candidates_no_pruning
                    if all(
                        (subset in set_promising_candidates)
                        for subset in combinations(selectors, curr_depth)
                    )
                )

                depth = depth + 1

        result = ps.prepare_subgroup_discovery_result(result, task)
        return ps.SubgroupDiscoveryResult(result, task)


class BestFirstSearch:
    def execute(self, task):
        result = []
        queue = [(float("-inf"), ps.Conjunction([]))]
        operator = ps.StaticSpecializationOperator(task.search_space)
        task.qf.calculate_constant_statistics(task.data, task.target)
        while queue:
            q, old_description = heappop(queue)
            q = -q
            if not q > ps.minimum_required_quality(result, task):
                break
            for candidate_description in operator.refinements(old_description):
                sg = candidate_description
                statistics = task.qf.calculate_statistics(sg, task.target, task.data)
                ps.add_if_required(
                    result,
                    sg,
                    task.qf.evaluate(sg, task.target, task.data, statistics),
                    task,
                    statistics=statistics,
                )
                if len(candidate_description) < task.depth:
                    if hasattr(task.qf, "optimistic_estimate"):
                        optimistic_estimate = task.qf.optimistic_estimate(
                            sg, task.target, task.data, statistics
                        )
                    else:
                        optimistic_estimate = np.inf

                    # compute refinements and fill the queue
                    if optimistic_estimate >= ps.minimum_required_quality(result, task):
                        if ps.constraints_satisfied(
                            task.constraints_monotone,
                            candidate_description,
                            statistics,
                            task.data,
                        ):
                            heappush(
                                queue, (-optimistic_estimate, candidate_description)
                            )

        result = ps.prepare_subgroup_discovery_result(result, task)
        return ps.SubgroupDiscoveryResult(result, task)


class GeneralisingBFS:  # pragma: no cover
    def __init__(self):
        self.alpha = 1.10
        self.discarded = [0, 0, 0, 0, 0, 0, 0]
        self.refined = [0, 0, 0, 0, 0, 0, 0]

    def execute(self, task):
        result = []
        queue = []
        operator = ps.StaticGeneralizationOperator(task.search_space)
        # init the first level
        for sel in task.search_space:
            queue.append((float("-inf"), ps.Disjunction([sel])))
        task.qf.calculate_constant_statistics(task.data, task.target)

        while queue:
            q, candidate_description = heappop(queue)
            q = -q
            if q < ps.minimum_required_quality(result, task):
                break

            sg = candidate_description
            statistics = task.qf.calculate_statistics(sg, task.target, task.data)
            quality = task.qf.evaluate(sg, task.target, task.data, statistics)
            ps.add_if_required(result, sg, quality, task, statistics=statistics)

            qual = ps.minimum_required_quality(result, task)

            if (quality, sg) in result:
                new_queue = []
                for q_tmp, c_tmp in queue:
                    if (-q_tmp) > qual:
                        heappush(new_queue, (q_tmp, c_tmp))
                queue = new_queue
            optimistic_estimate = task.qf.optimistic_estimate(
                sg, task.target, task.data, statistics
            )
            # else:
            #    ps.add_if_required(
            #       result, sg, task.qf.evaluate_from_dataset(task.data, sg), task)
            #    optimistic_estimate = task.qf.optimistic_generalisation_from_dataset(
            #       task.data, sg) if qf_is_bounded else float("inf")

            # compute refinements and fill the queue
            if len(candidate_description) < task.depth and (
                optimistic_estimate / self.alpha ** (len(candidate_description) + 1)
            ) >= ps.minimum_required_quality(result, task):
                # print(qual)
                # print(optimistic_estimate)
                self.refined[len(candidate_description)] += 1
                # print(str(candidate_description))
                for new_description in operator.refinements(candidate_description):
                    heappush(queue, (-optimistic_estimate, new_description))
            else:
                self.discarded[len(candidate_description)] += 1

        result.sort(key=lambda x: x[0], reverse=True)
        print("discarded " + str(self.discarded))
        return ps.SubgroupDiscoveryResult(result, task)


class BeamSearch:
    """
    Implements the BeamSearch algorithm. Its a basic implementation
    """

    class PoolArgs(object):
        def __init__(self, last_sg, selector_idx):
            super().__init__()
            self.last_sg = last_sg
            self.selector_idx = selector_idx

    class PoolResult(object):
        def __init__(self, sg, quality, statistics):
            super().__init__()
            self.sg_inds = sg
            self.quality = quality
            self.statistics = statistics

    def init_worker(task):
        BeamSearch.task = task
        # BeamSearch.task.search_space = BeamSearch.task.search_space.copy()
        BeamSearch.task.data = BeamSearch.task.data.copy() # copying the data, releases some bottleneck for multiprocessing

    task = None

    def __init__(self, beam_width=20, beam_width_adaptive=False, nproc=0):
        self.beam_width = beam_width
        self.beam_width_adaptive = beam_width_adaptive
        self.nproc = nproc

    def _process_subgroup(self, args):
        # print('worker, id(task)={}'.format(id(BeamSearch.task)))
        last_sg = args.last_sg
        task = BeamSearch.task

        if args.selector_idx in last_sg:
            return None
        # if sel in last_sg.selectors:
        #     return None

        sg_inds = last_sg + [args.selector_idx,]
        sg = ps.sg_from_inds(task.search_space, sg_inds)
        # sg = ps.Conjunction(last_sg.selectors + (sel,))
        statistics = task.qf.calculate_statistics(
            sg, task.target, task.data
        )
        quality = task.qf.evaluate(sg, task.target, task.data, statistics)

        return BeamSearch.PoolResult(sg_inds, quality, statistics)

    def execute(self, task):
        # adapt beam width to the result set size if desired
        beam_width = self.beam_width
        if self.beam_width_adaptive:
            beam_width = task.result_set_size

        # check if beam size is to small for result set
        if beam_width < task.result_set_size:
            raise RuntimeError(
                "Beam width in the beam search algorithm "
                "is smaller than the result set size!"
            )

        task.qf.calculate_constant_statistics(task.data, task.target)

        visited = set()

        # init
        beam = [
            (
                0,
                [], # selectors
                task.qf.calculate_statistics(slice(None), task.target, task.data),
            )
        ]
        previous_beam = None

        pool = None
        if self.nproc > 1:
            pool = Pool(self.nproc, initializer=BeamSearch.init_worker, initargs=(task,))

        dt_map = 0
        dt_add_if = 0

        depth = 0
        while beam != previous_beam and depth < task.depth:
            previous_beam = beam.copy()

            for _, last_sg_inds, _ in previous_beam:
                # sg_hash = str(sorted(last_sg_inds))
                # if sg_hash in visited:
                #     continue
                # visited.add(sg_hash)

                # if getattr(last_sg, "visited", False):
                #     continue
                # setattr(last_sg, "visited", True)

                if pool is not None:
                    # results = pool.map
                    dt_map_t0 = time.time()
                    dt_add_if_ = 0
                    for result in pool.imap_unordered(self._process_subgroup, [BeamSearch.PoolArgs(last_sg_inds, i) for i in range(len(task.search_space))], chunksize=20): #len(task.search_space)//self.nproc):
                        if result is None:
                            continue

                        dt = time.time()
                        ps.add_if_required(
                            beam,
                            visited,
                            result.sg_inds,
                            result.quality,
                            task,
                            check_for_duplicates=True,
                            statistics=result.statistics,
                            explicit_result_set_size=beam_width,
                        )
                        dt_add_if_ += time.time() - dt

                    dt_map = (time.time() - dt_map_t0) - dt_add_if_
                    # dt_add_if = dt_add_if_

                    # print('dt for map: ', dt_map)
                    # print('dt for add_if: ', dt_add_if_)
                else:
                    for sel in task.search_space:
                        # create a clone
                        if sel in last_sg.selectors:
                            continue
                        sg = ps.Conjunction(last_sg.selectors + (sel,))
                        statistics = task.qf.calculate_statistics(
                            sg, task.target, task.data
                        )
                        quality = task.qf.evaluate(sg, task.target, task.data, statistics)
                        ps.add_if_required(
                            beam,
                            sg,
                            quality,
                            task,
                            check_for_duplicates=True,
                            statistics=statistics,
                            explicit_result_set_size=beam_width,
                        )
            depth += 1
            print('BeamSearch depth: [{}/{}]'.format(depth, task.depth))

        if pool is not None:
            pool.close()

            # convert results
            for i in range(len(beam)):
                beam[i] = (beam[i][0], ps.sg_from_inds(task.search_space, beam[i][1]), beam[i][2])

        # result = beam[-task.result_set_size:]
        while len(beam) > task.result_set_size:
            heappop(beam)

        result = beam
        result = ps.prepare_subgroup_discovery_result(result, task)
        return ps.SubgroupDiscoveryResult(result, task)


    def _execute(self, task):
        # adapt beam width to the result set size if desired
        beam_width = self.beam_width
        if self.beam_width_adaptive:
            beam_width = task.result_set_size

        # check if beam size is to small for result set
        if beam_width < task.result_set_size:
            raise RuntimeError(
                "Beam width in the beam search algorithm "
                "is smaller than the result set size!"
            )

        task.qf.calculate_constant_statistics(task.data, task.target)

        # init
        beam = [
            (
                0,
                ps.Conjunction([]),
                task.qf.calculate_statistics(slice(None), task.target, task.data),
            )
        ]
        previous_beam = None

        depth = 0
        while beam != previous_beam and depth < task.depth:
            previous_beam = beam.copy()
            for _, last_sg, _ in previous_beam:
                if getattr(last_sg, "visited", False):
                    continue
                setattr(last_sg, "visited", True)
                for sel in task.search_space:
                    # create a clone
                    if sel in last_sg.selectors:
                        continue
                    sg = ps.Conjunction(last_sg.selectors + (sel,))
                    statistics = task.qf.calculate_statistics(
                        sg, task.target, task.data
                    )
                    quality = task.qf.evaluate(sg, task.target, task.data, statistics)
                    ps.add_if_required(
                        beam,
                        sg,
                        quality,
                        task,
                        check_for_duplicates=True,
                        statistics=statistics,
                        explicit_result_set_size=beam_width,
                    )
            depth += 1

        # result = beam[-task.result_set_size:]
        while len(beam) > task.result_set_size:
            heappop(beam)

        result = beam
        result = ps.prepare_subgroup_discovery_result(result, task)
        return ps.SubgroupDiscoveryResult(result, task)


class SimpleSearch:
    def __init__(self, show_progress=True):
        self.show_progress = show_progress

    def execute(self, task):
        task.qf.calculate_constant_statistics(task.data, task.target)
        result = []
        all_selectors = chain.from_iterable(
            combinations(task.search_space, r) for r in range(1, task.depth + 1)
        )
        if self.show_progress:
            try:
                from tqdm.auto import tqdm  # pylint: disable=import-outside-toplevel

                def binomial(x, y):
                    try:
                        binom = factorial(x) // factorial(y) // factorial(x - y)
                    except ValueError:  # pragma: no cover
                        binom = 0
                    return binom

                total = sum(
                    binomial(len(task.search_space), k)
                    for k in range(1, task.depth + 1)
                )
                all_selectors = tqdm(all_selectors, total=total)
            except ImportError:  # pragma: no cover
                warnings.warn(
                    "tqdm not installed but show_progress=True", ImportWarning
                )
        for selectors in all_selectors:
            sg = ps.Conjunction(selectors)
            statistics = task.qf.calculate_statistics(sg, task.target, task.data)
            quality = task.qf.evaluate(sg, task.target, task.data, statistics)
            ps.add_if_required(result, sg, quality, task, statistics=statistics)
        result = ps.prepare_subgroup_discovery_result(result, task)
        return ps.SubgroupDiscoveryResult(result, task)


class SimpleDFS:
    def execute(self, task, use_optimistic_estimates=True):
        task.qf.calculate_constant_statistics(task.data, task.target)
        result = self.search_internal(
            task, [], task.search_space, [], use_optimistic_estimates
        )
        result = ps.prepare_subgroup_discovery_result(result, task)
        return ps.SubgroupDiscoveryResult(result, task)

    def search_internal(
        self, task, prefix, modification_set, result, use_optimistic_estimates
    ):
        sg = ps.Conjunction(copy.copy(prefix))

        statistics = task.qf.calculate_statistics(sg, task.target, task.data)
        if (
            use_optimistic_estimates
            and len(prefix) < task.depth
            and isinstance(task.qf, ps.BoundedInterestingnessMeasure)
        ):
            optimistic_estimate = task.qf.optimistic_estimate(
                sg, task.target, task.data, statistics
            )
            if not optimistic_estimate > ps.minimum_required_quality(result, task):
                return result

        quality = task.qf.evaluate(sg, task.target, task.data, statistics)
        ps.add_if_required(result, sg, quality, task, statistics=statistics)
        if not ps.constraints_satisfied(
            task.constraints_monotone, sg, statistics=statistics, data=task.data
        ):
            return result
        if len(prefix) < task.depth:
            new_modification_set = copy.copy(modification_set)
            for sel in modification_set:
                prefix.append(sel)
                new_modification_set.pop(0)
                self.search_internal(
                    task, prefix, new_modification_set, result, use_optimistic_estimates
                )
                # remove the sel again
                prefix.pop(-1)
        return result


class DFS:
    """
    Implementation of a depth-first-search
    with look-ahead using a provided datastructure.
    """

    def __init__(self, apply_representation=None):
        self.target_bitset = None
        if apply_representation is None:
            apply_representation = ps.BitSetRepresentation
        self.apply_representation = apply_representation
        self.operator = None
        self.params_tpl = namedtuple(
            "StandardQF_parameters", ("size_sg", "positives_count")
        )

    def execute(self, task):
        self.operator = ps.StaticSpecializationOperator(task.search_space)
        task.qf.calculate_constant_statistics(task.data, task.target)
        result = []
        with self.apply_representation(task.data, task.search_space) as representation:
            self.search_internal(task, result, representation.Conjunction([]))
        result = ps.prepare_subgroup_discovery_result(result, task)
        return ps.SubgroupDiscoveryResult(result, task)

    def search_internal(self, task, result, sg):
        statistics = task.qf.calculate_statistics(sg, task.target, task.data)
        if not constraints_satisfied(
            task.constraints_monotone, sg, statistics, task.data
        ):
            return
        optimistic_estimate = task.qf.optimistic_estimate(
            sg, task.target, task.data, statistics
        )
        if not optimistic_estimate > ps.minimum_required_quality(result, task):
            return
        quality = task.qf.evaluate(sg, task.target, task.data, statistics)
        ps.add_if_required(result, sg, quality, task, statistics=statistics)

        if sg.depth < task.depth:
            for new_sg in self.operator.refinements(sg):
                self.search_internal(task, result, new_sg)


class DFSNumeric:
    tpl = namedtuple("size_mean_parameters", ("size_sg", "mean"))

    def __init__(self):
        self.pop_size = 0
        self.f = None
        self.target_values = None
        self.bitsets = {}
        self.num_calls = 0
        self.evaluate = None

    def execute(self, task):
        if not isinstance(task.qf, ps.StandardQFNumeric):
            raise RuntimeError(
                "BSD_numeric so far is only implemented for StandardQFNumeric"
            )
        self.pop_size = len(task.data)
        sorted_data = task.data.sort_values(
            task.target.get_attributes()[0], ascending=False
        )

        # generate target bitset
        self.target_values = sorted_data[task.target.get_attributes()[0]].to_numpy()

        task.qf.calculate_constant_statistics(task.data, task.target)

        # generate selector bitsets
        self.bitsets = {}
        for sel in task.search_space:
            # generate bitset
            self.bitsets[sel] = sel.covers(sorted_data)
        result = self.search_internal(
            task, [], task.search_space, [], np.ones(len(sorted_data), dtype=bool)
        )
        result = ps.prepare_subgroup_discovery_result(result, task)
        return ps.SubgroupDiscoveryResult(result, task)

    def search_internal(self, task, prefix, modification_set, result, bitset):
        self.num_calls += 1
        sg_size = bitset.sum()
        if sg_size == 0:
            return result
        target_values_sg = self.target_values[bitset]

        target_values_cs = np.cumsum(target_values_sg, dtype=np.float64)

        sizes = np.arange(1, len(target_values_cs) + 1)
        mean_values_cs = target_values_cs / sizes
        tpl = DFSNumeric.tpl(sizes, mean_values_cs)
        qualities = task.qf.evaluate(None, None, None, tpl)
        optimistic_estimate = np.max(qualities)

        if optimistic_estimate <= ps.minimum_required_quality(result, task):
            return result

        sg = ps.Conjunction(copy.copy(prefix))

        quality = qualities[-1]
        ps.add_if_required(result, sg, quality, task)

        if len(prefix) < task.depth:
            new_modification_set = copy.copy(modification_set)
            for sel in modification_set:
                prefix.append(sel)
                new_bitset = bitset & self.bitsets[sel]
                new_modification_set.pop(0)
                self.search_internal(
                    task, prefix, new_modification_set, result, new_bitset
                )
                # remove the sel again
                prefix.pop(-1)
        return result
