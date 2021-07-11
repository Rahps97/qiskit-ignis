 # -*- coding: utf-8 -*-

# This code is part of Qiskit.
#
# (C) Copyright IBM 2019.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

# pylint: disable=invalid-name

"""
Decoders for quantum error correction codes, with a focus on those that can be
expressed as solving a graph-theoretic problem.
"""

import copy
import warnings
import retworkx as rx
import numpy as np

from qiskit import QuantumCircuit, execute
from sklearn.cluster import DBSCAN

try:
    from qiskit.providers.aer import Aer
    HAS_AER = True
except ImportError:
    from qiskit import BasicAer
    HAS_AER = False


class GraphDecoder():
    """
    Class to construct the graph corresponding to the possible syndromes
    of a quantum error correction code, and then run suitable decoders.
    """

    def __init__(self, code, S=None):
        """
        Args:
            code (RepitionCode): The QEC Code object for which this decoder
                will be used.
            S (retworkx.PyGraph): Graph describing connectivity between syndrome
                elements. Will be generated automatically if not supplied.

        Additional information:
            The decoder for the supplied ``code`` is initialized by running
            ``_make_syndrome_graph()``. Since this process can take some
            time, it is also possible to load in a premade ``S``. However,
            if this was created for a differently defined ``code``, it won't
            work properly.
        """

        self.code = code

        if S:
            self.S = S
        else:
            self.S = self._make_syndrome_graph()

    def _separate_string(self, string):

        separated_string = []
        for syndrome_type_string in string.split('  '):
            separated_string.append(syndrome_type_string.split(' '))
        return separated_string

    def _string2nodes(self, string):

        separated_string = self._separate_string(string)
        nodes = []
        for syn_type, _ in enumerate(separated_string):
            for syn_round in range(
                    len(separated_string[syn_type])):
                elements = \
                    separated_string[syn_type][syn_round]
                for elem_num, element in enumerate(elements):
                    if element == '1':
                        nodes.append((syn_type,
                                      syn_round,
                                      elem_num))
        return nodes

    def _make_syndrome_graph(self):
        """
        This method injects all possible Pauli errors into the circuit for
        ``code``.

        This is done by examining the qubits used in each gate of the
        circuit for a stored logical 0. A graph is then created with a node
        for each non-trivial syndrome element, and an edge between all such
        elements that can be created by the same error.
        """

        S = rx.PyGraph(multigraph=False)

        qc = self.code.circuit['0']

        blank_qc = QuantumCircuit()
        for qreg in qc.qregs:
            blank_qc.add_register(qreg)
        for creg in qc.cregs:
            blank_qc.add_register(creg)

        error_circuit = {}
        circuit_name = {}
        depth = len(qc)
        for j in range(depth):
            qubits = qc.data[j][1]
            for qubit in qubits:
                for error in ['x', 'y', 'z']:
                    temp_qc = copy.deepcopy(blank_qc)
                    temp_qc.name = str((j, qubit, error))
                    temp_qc.data = qc.data[0:j]
                    getattr(temp_qc, error)(qubit)
                    temp_qc.data += qc.data[j:depth + 1]
                    circuit_name[(j, qubit, error)] = temp_qc.name
                    error_circuit[temp_qc.name] = temp_qc

        if HAS_AER:
            simulator = Aer.get_backend('qasm_simulator')
        else:
            simulator = BasicAer.get_backend('qasm_simulator')

        job = execute(list(error_circuit.values()), simulator)

        node_map = {}
        for j in range(depth):
            qubits = qc.data[j][1]
            for qubit in qubits:
                for error in ['x', 'y', 'z']:
                    raw_results = {}
                    raw_results['0'] = job.result().get_counts(
                        str((j, qubit, error)))
                    results = self.code.process_results(raw_results)['0']

                    for string in results:

                        nodes = self._string2nodes(string)

                        assert len(nodes) in [0, 2], "Error of type " + \
                            error + " on qubit " + str(qubit) + \
                            " at depth " + str(j) + " creates " + \
                            str(len(nodes)) + \
                            " nodes in syndrome graph, instead of 2."
                        for node in nodes:
                            if node not in node_map:
                                node_map[node] = S.add_node(node)
                        for source in nodes:
                            for target in nodes:
                                if target != source:
                                    S.add_edge(node_map[source],
                                               node_map[target], 1)

        return S

    def get_error_probs(self, results):
        """
        Generate probabilities of single error events from result counts.

        Args:
            results (dict): A results dictionary, as produced by the
            `process_results` method of the code.

        Returns:
            dict: Keys are the edges for specific error
            events, and values are the calculated probabilities

        Additional information:
            Uses `results` to estimate the probability of the errors that
            create the pairs of nodes specified by the edge.
            Calculation done using the method of Spitz, et al.
            https://doi.org/10.1002/qute.201800012
        """

        results = results['0']
        shots = sum(results.values())

        error_probs = {}
        for edge in self.S.edge_list():

            # initialize averages
            av_vv = 0  # v_ij
            av_v = [0, 0]  # [v_,v_j]
            av_xor = 0  # v_{i xor j}

            for string in results:

                # list of i for which v_i=1
                error_nodes = self._string2nodes(string)

                # get [v_i,v_j] for edge (i,j)
                v = [int(self.S[edge[k]] in error_nodes) for k in range(2)]

                # update averages
                av_vv += v[0]*v[1]*results[string]
                for k in range(2):
                    av_v[k] += v[k]*results[string]
                av_xor += (v[0] != v[1])*results[string]

            # normalize
            av_vv /= shots
            av_v[0] /= shots
            av_v[1] /= shots
            av_xor /= shots

            if (1 - 2*av_xor) != 0:
                x = (av_vv - av_v[0]*av_v[1])/(1 - 2*av_xor)
            else:
                x = np.nan
            error_probs[self.S[edge[0]], self.S[edge[1]]] = max(0, 0.5 - np.sqrt(0.25-x))

        return error_probs

    def weight_syndrome_graph(self, results):
        """Generate weighted syndrome graph from result counts.

        Args:
            results (dict): A results dictionary, as produced by the
            `process_results` method of the code.

        Additional information:
            Uses `results` to estimate the probability of the errors that
            create the pairs of nodes in S. The edge weights are then
            replaced with the corresponding -log(p/(1-p).
        """

        error_probs = self.get_error_probs(results)

        for edge in self.S.edge_list():
            p = error_probs[self.S[edge[0]], self.S[edge[1]]]
            if p == 0:
                w = np.inf
            elif 1-p == 1:
                w = -np.inf
            else:
                w = -np.log(p/(1-p))
            self.S.update_edge(edge[0], edge[1], w)

    def make_error_graph(self, string, subgraphs=None):
        """
        Args:
            string (str): A string describing the output from the code.
            subgraphs (list): Used when multiple, semi-independent graphs need
            need to created.

        Returns:
            E: The subgraph(s) of S which corresponds to the non-trivial
            syndrome elements in the given string.
        """

        if subgraphs is None:
            subgraphs = []
            for syndrome_type in string.split('  '):
                subgraphs.append(['0'])

        set_subgraphs = [
            subgraph for subs4type in subgraphs for subgraph in subs4type]

        E = {}
        node_sets = {}
        for subgraph in set_subgraphs:
            E[subgraph] = rx.PyGraph(multigraph=False)
            node_sets[subgraph] = set()

        E = {subgraph: rx.PyGraph(multigraph=False) for subgraph in set_subgraphs}
        separated_string = self._separate_string(string)
        for syndrome_type, _ in enumerate(separated_string):
            for syndrome_round in range(len(separated_string[syndrome_type])):
                elements = separated_string[syndrome_type][syndrome_round]
                for elem_num, element in enumerate(elements):
                    if element == '1' or syndrome_type == 0:
                        for subgraph in subgraphs[syndrome_type]:
                            node_data = (syndrome_type, syndrome_round, elem_num)
                            if node_data not in node_sets[subgraph]:
                                E[subgraph].add_node(node_data)
                                node_sets[subgraph].add(node_data)

        # for each pair of nodes in error create an edge and weight with the
        # distance
        distance_matrix = rx.graph_floyd_warshall_numpy(self.S, weight_fn=float)
        s_node_map = {self.S[index]: index for index in self.S.node_indexes()}

        for subgraph in set_subgraphs:
            for source_index in E[subgraph].node_indexes():
                for target_index in E[subgraph].node_indexes():
                    source = E[subgraph][source_index]
                    target = E[subgraph][target_index]
                    if target != source:
                        distance = int(distance_matrix[s_node_map[source]][s_node_map[target]])
                        E[subgraph].add_edge(source_index, target_index,
                                             -distance)
        return E
    def matching(self, string):
        """
        Args:
            string (str): A string describing the output from the code.

        Returns:
            str: A string with corrected logical values,
                computed using minimum weight perfect matching.

        Additional information:
            This function can be run directly, or used indirectly to
            calculate a logical error probability with `get_logical_prob`
        """

        # this matching algorithm is designed for a single graph
        E = self.make_error_graph(string)['0']

        # set up graph that is like E, but each syndrome node is connected to a
        # separate copy of the nearest logical node
        E_matching = rx.PyGraph(multigraph=False)
        syndrome_nodes = []
        logical_nodes = []
        logical_neighbours = []
        node_map = {}
        for node in E.nodes():
            node_map[node] = E_matching.add_node(node)
            if node[0] == 0:
                logical_nodes.append(node)
            else:
                syndrome_nodes.append(node)
        for source in syndrome_nodes:
            for target in syndrome_nodes:
                if target != (source):
                    E_matching.add_edge(
                        node_map[source],
                        node_map[target],
                        E.get_edge_data(node_map[source],
                                        node_map[target]))

            potential_logical = {}
            for target in logical_nodes:
                potential_logical[target] = E.get_edge_data(node_map[source],
                                                            node_map[target])
            nearest_logical = max(potential_logical, key=potential_logical.get)
            nl_target = nearest_logical + source
            if nl_target not in node_map:
                node_map[nl_target] = E_matching.add_node(nl_target)
            E_matching.add_edge(
                node_map[source],
                node_map[nl_target],
                potential_logical[nearest_logical])
            logical_neighbours.append(nl_target)
        for source in logical_neighbours:
            for target in logical_neighbours:
                if target != (source):
                    E_matching.add_edge(node_map[source], node_map[target], 0)
        # do the matching on this
        matches = {
            (E_matching[x[0]],
             E_matching[x[1]]) for x in rx.max_weight_matching(
                 E_matching, max_cardinality=True, weight_fn=lambda x: x)}
        # use it to construct and return a corrected logical string
        logicals = self._separate_string(string)[0]
        for (source, target) in matches:
            if source[0] == 0 and target[0] != 0:
                logicals[source[1]] = str((int(logicals[source[1]]) + 1) % 2)
            if target[0] == 0 and source[0] != 0:
                logicals[target[1]] = str((int(logicals[target[1]]) + 1) % 2)

        logical_string = ''
        for logical in logicals:
            logical_string += logical + ' '
        logical_string = logical_string[:-1]

        return logical_string
    def cluster_decoding(self,string, eps = 4):
        """
            Args:
                string (str): A string describing the output from the code.
                eps (int):The maximum distance between two samples for one
                to be considered as in the neighborhood of the other. This
                is not a maximum bound on the distances of points within a
                cluster. This is the most important DBSCAN parameter to
                choose appropriately for your data set and distance function.
                Default value here is 4.
            Returns:
                str: A string with corrected logical values,
                    computed using clustering and matching.
            Additional information:
                This function can be run directly, or used indirectly to
                calculate a logical error probability with `get_logical_prob
        """
        Error_Graph = self.make_error_graph(string)['0']
        logical_nodes = [(0,0,0),(0,1,0)]
        Non_neutral_nodes = list(Error_Graph.nodes())
        for _ in logical_nodes:
            Non_neutral_nodes.remove(_)
        # Trivial Case
        if len(Non_neutral_nodes) == 0:
            logicals = self._separate_string(string)[0]
            logical_string = ''
            for logical in logicals:
                logical_string += logical + ' '
            logical_string = logical_string[:-1]
            return logical_string
        #Cluster Decoder
        corrected_logical_string = []
        Clustering = DBSCAN(eps = eps, min_samples = 2,
                            metric = 'manhattan').fit(Non_neutral_nodes)
        Cluster ={_:[] for _ in set(Clustering.labels_)}
        for _ , __ in zip(Clustering.labels_,Non_neutral_nodes):
            Cluster[_].append(__)
        ### appending logical nodes as separate clusters
        Cluster['logical_0'] = [logical_nodes[0]]
        Cluster['logical_1'] = [logical_nodes[1]]
        Unmatched_node = True
        while Unmatched_node:
            for _ in Cluster.keys():
                if len(Cluster[_]) % 2 != 0 and _ != 'logical_0' and _ != 'logical_1':
                    S = Nearest_Cluster(Cluster, Error_Graph, _)
                    if S[1] == 'logical_0' or S[1] == 'logical_1':
                        corrected_logical_string.append(S[1][-1])
                        Cluster[_].append(S[0])
                    else:
                        Cluster[_] = Cluster[_] + Cluster[S[1]]
                        Cluster.pop(S[1])
                        break
                else:
                    Unmatched_node = False
        neutral_nodelist = []
        Edgelist = []
        for _ in Cluster.keys():
            Cluster_Graph = rx.PyGraph()
            Cluster_Graph.add_nodes_from(Error_Graph.nodes())
            Cluster_Graph.add_edges_from(Error_Graph.weighted_edge_list())
            for i, __ in enumerate(Error_Graph.nodes()):
                if __ not in Cluster[_]:
                    Cluster_Graph.remove_node(i)
            Edges = [(Cluster_Graph[x[0]],
             Cluster_Graph[x[1]]) for x in rx.max_weight_matching(
                 Cluster_Graph, max_cardinality=True, weight_fn=lambda x: x)]
            Edgelist = Edgelist + Edges
            neutral_nodelist += [k[0] for k in list(Edges)] + [k[1] for k in list(Edges)]
        # use it to construct and return a corrected logical string
        logicals = self._separate_string(string)[0]
        for (source, target) in Edgelist:
            if source[0] == 0 and target[0] != 0:
                logicals[source[1]] = str((int(logicals[source[1]]) + 1) % 2)
            if target[0] == 0 and source[0] != 0:
                logicals[target[1]] = str((int(logicals[target[1]]) + 1) % 2)
        logical_string = ''
        for logical in logicals:
            logical_string += logical + ' '
        logical_string = logical_string[:-1]
        return [logical_string,Edgelist,neutral_nodelist]
    def get_logical_prob(self, results, algorithm = 'matching'):
        """
        Args:
            results (dict): A results dictionary, as produced by the
            `process_results` method of the code.
            algorithm (str): Choice of which decoder to use.
        Returns:
            dict: Dictionary of logical error probabilities for
            each of the encoded logical states whose results were given in
            the input.
        """

        logical_prob = {}
        for log in results:

            shots = 0
            incorrect_shots = 0

            corrected_results = {}
            if algorithm == 'matching':
                for string in results[log]:
                    corr_str = self.matching(string)
                    if corr_str in corrected_results:
                        corrected_results[corr_str] += results[log][string]
                    else:
                        corrected_results[corr_str] = results[log][string]
            elif algorithm == 'clustering':
                for string in results[log]:
                    corr_str = self.cluster_decoding(string)[0]
                    if corr_str in corrected_results:
                        corrected_results[corr_str] += results[log][string]
                    else:
                        corrected_results[corr_str] = results[log][string]
            else:
                warnings.warn(
                    "The requested algorithm " +
                    str(algorithm) +
                    " is not known.",
                    Warning)

            for string in corrected_results:
                shots += corrected_results[string]
                if string[0] != str(log):
                    incorrect_shots += corrected_results[string]

            logical_prob[log] = incorrect_shots / shots

        return logical_prob


def Nearest_Cluster(Cluster, Error_Graph, target ):
    """
        Args:
            Cluster (dict): Dictionary that contains clusters in the
            Error graph and the nodes in it.
            Error_graph (Networkx Graph):Error graph in which the
            nearest cluster and the node will be searched
            target (int,int,int) : target cluster for which nearest
            cluster is being searched
        Returns:
            list : [nearest_outside_node, nearest_cluster]
            nearest_outside_node : nearest node to the target node
            which doesn't belong to the same cluster
            nearest_cluster : cluster to which nearest outside node
            belongs
    """
    Cluster_Graph = rx.PyGraph()
    Cluster_Graph.add_nodes_from(Error_Graph.nodes())
    Cluster_Graph.add_edges_from(Error_Graph.weighted_edge_list())
    for i, __ in enumerate(Error_Graph.nodes()):
        if __ not in Cluster[target]:
            Cluster_Graph.remove_node(i)
    Edges =  rx.max_weight_matching(Cluster_Graph,
             max_cardinality=True, weight_fn=lambda x: x)
    remaining_node =  list(Cluster_Graph.node_indexes())
    for edge in Edges:
        remaining_node.remove(edge[0])
        remaining_node.remove(edge[1])
    node_neigbours = {}
    for edge in Error_Graph.weighted_edge_list():
        if remaining_node[0] == edge[0]:
            node_neigbours[Error_Graph[edge[1]]] = {'weight': edge[2]}
        if remaining_node[0] == edge[1]:
            node_neigbours[Error_Graph[edge[0]]] = {'weight': edge[2]}
    nearest_neighbours = sorted(node_neigbours.items(),
                            key=lambda e: e[1]["weight"],
                            reverse = True)[:len(Cluster[target])]
    nearest_outside_node = [x[0] for x in nearest_neighbours if x[0] not in
                            Cluster[target]]
    for x in Cluster.keys():
        if nearest_outside_node[0] in Cluster[x]:
            nearest_cluster = x
    return [nearest_outside_node[0], nearest_cluster]


def postselection_decoding(results):
    """
    Calculates the logical error probability using postselection decoding.

    This postselects all results with trivial syndrome.

    Args:
        results (dict): A results dictionary, as produced by the
            `process_results` method of a code.

    Returns:
        dict: Dictionary of logical error probabilities for
           each of the encoded logical states whose results were given in
           the input.
    """
    logical_prob = {}
    postselected_results = {}
    for log in results:
        postselected_results[log] = {}
        for string in results[log]:

            syndrome_list = string.split('  ')
            syndrome_list.pop(0)
            syndrome_string = '  '.join(syndrome_list)

            error_free = True
            for char in syndrome_string:
                error_free = error_free and (char in ['0', ' '])

            if error_free:
                postselected_results[log][string] = results[log][string]

    for log in results:
        shots = 0
        incorrect_shots = 0
        for string in postselected_results[log]:
            shots += postselected_results[log][string]
            if string[0] != log:
                incorrect_shots += postselected_results[log][string]

        logical_prob[log] = incorrect_shots / shots

    return logical_prob


def lookuptable_decoding(training_results, real_results):
    """
    Calculates the logical error probability using postselection decoding.
    This postselects all results with trivial syndrome.

    Args:
        training_results (dict): A results dictionary, as produced by the
            ``process_results`` method of a code.
        real_results (dict): A results dictionary, as produced by the
            ``process_results`` method of a code.

    Returns:
        dict: Dictionary of logical error probabilities for
            each of the encoded logical states whose results were given in
            the input.


    Additional information:
        Given a two dictionaries of results, as produced by a code object,
        thelogical error probability is calculated for lookup table
        decoding. This is done using `training_results` as a guide to which
        syndrome is most probable for each logical value, and the
        probability is calculated for the results in `real_results`.
    """

    logical_prob = {}
    for log in real_results:
        shots = 0
        incorrect_shots = 0
        for string in real_results[log]:

            p = {}
            for testlog in ['0', '1']:
                if string in training_results[testlog]:
                    p[testlog] = training_results[testlog][string]
                else:
                    p[testlog] = 0

            shots += real_results[log][string]
            if p['1' * (log == '0') + '0' * (log == '1')] > p[log]:
                incorrect_shots += real_results[log][string]

        logical_prob[log] = incorrect_shots / shots

    return logical_prob
