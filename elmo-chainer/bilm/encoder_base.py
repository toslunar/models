# from typing import Tuple, Union, Optional, Callable

import numpy as np

import chainer
from chainer import cuda
from chainer import functions as F
from chainer import links as L
from chainer import Variable

ConfigurationError = ValueError

# We have two types here for the state, because storing the state in something
# which is Iterable (like a tuple, below), is helpful for internal manipulation
# - however, the states are consumed as either Tensors or a Tuple of Tensors, so
# returning them in this format is unhelpful.
# RnnState = Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]  # pylint: disable=invalid-name
# RnnStateStorage = Tuple[torch.Tensor, ...]  # pylint: disable=invalid-name


def get_lengths_from_binary_sequence_mask(mask):
    # (batch_size, sequence_length) -> (batch_size,)
    return mask.sum(-1)


def argsort_list_descent(lengths):
    return np.argsort([-l for l in lengths]).astype('i')


def permutate_list(lst, indices, inv):
    ret = [None] * len(lst)
    if inv:
        for i, ind in enumerate(indices):
            ret[ind] = lst[i]
    else:
        for i, ind in enumerate(indices):
            ret[i] = lst[ind]
    return ret


def sort_batch_by_length(tensor,
                         sequence_lengths):
    """
    Sort a batch first tensor by some specified lengths.
    Parameters
    ----------
    tensor : Variable(torch.FloatTensor), required.
        A batch first Pytorch tensor.
    sequence_lengths : Variable(torch.LongTensor), required.
        A tensor representing the lengths of some dimension of the tensor which
        we want to sort by.
    Returns
    -------
    sorted_tensor : Variable(torch.FloatTensor)
        The original tensor sorted along the batch dimension with respect to sequence_lengths.
    sorted_sequence_lengths : Variable(torch.LongTensor)
        The original sequence_lengths sorted by decreasing size.
    restoration_indices : Variable(torch.LongTensor)
        Indices into the sorted_tensor such that
        ``sorted_tensor.index_select(0, restoration_indices) == original_tensor``
    permuation_index : Variable(torch.LongTensor)
        The indices used to sort the tensor. This is useful if you want to sort many
        tensors using the same ordering.
    """

    """
    if not isinstance(tensor, Variable) or not isinstance(sequence_lengths, Variable):
        raise ConfigurationError(
            "Both the tensor and sequence lengths must be torch.autograd.Variables.")
    """
    sorted_sequence_lengths = np.sort(sequence_lengths, 0)[::-1]
    permutation_index = np.argsort(sequence_lengths, 0)[::-1]
    sorted_tensor = tensor[permutation_index]

    index_range = np.arange(len(sequence_lengths))
    reverse_mapping = permutation_index[::-1]
    restoration_indices = index_range[reverse_mapping]
    return sorted_tensor, sorted_sequence_lengths, restoration_indices, permutation_index


class _EncoderBase(chainer.Chain):
    """
    This abstract class serves as a base for the 3 ``Encoder`` abstractions in AllenNLP.
    - :class:`~allennlp.modules.seq2seq_encoders.Seq2SeqEncoders`
    - :class:`~allennlp.modules.seq2vec_encoders.Seq2VecEncoders`

    Additionally, this class provides functionality for sorting sequences by length
    so they can be consumed by Pytorch RNN classes, which require their inputs to be
    sorted by length. Finally, it also provides optional statefulness to all of it's
    subclasses by allowing the caching and retrieving of the hidden states of RNNs.
    """

    def __init__(self, stateful=False):
        super(_EncoderBase, self).__init__()
        self.stateful = stateful
        self._states = None

    def sort_and_run_forward(self,
                             module,
                             inputs,
                             mask,
                             hidden_state=None):
        """
        Parameters
        ----------
        module : ``Callable[[PackedSequence, Optional[RnnState]],
                            Tuple[Union[PackedSequence, torch.Tensor], RnnState]]``, required.
            A function to run on the inputs. In most cases, this is a ``torch.nn.Module``.
        inputs : ``torch.Tensor``, required.
            A tensor of shape ``(batch_size, sequence_length, embedding_size)`` representing
            the inputs to the Encoder.
        mask : ``torch.Tensor``, required.
            A tensor of shape ``(batch_size, sequence_length)``, representing masked and
            non-masked elements of the sequence for each element in the batch.
        hidden_state : ``Optional[RnnState]``, (default = None).
            A single tensor of shape (num_layers, batch_size, hidden_size) representing the
            state of an RNN with or a tuple of
            tensors of shapes (num_layers, batch_size, hidden_size) and
            (num_layers, batch_size, memory_size), representing the hidden state and memory
            state of an LSTM-like RNN.

        Returns
        -------
        module_output : ``Union[torch.Tensor, PackedSequence]``.
            A Tensor or PackedSequence representing the output of the Pytorch Module.
            The batch size dimension will be equal to ``num_valid``, as sequences of zero
            length are clipped off before the module is called, as Pytorch cannot handle
            zero length sequences.
        final_states : ``Optional[RnnState]``
            A Tensor representing the hidden state of the Pytorch Module. This can either
            be a single tensor of shape (num_layers, num_valid, hidden_size), for instance in
            the case of a GRU, or a tuple of tensors, such as those required for an LSTM.
        restoration_indices : ``torch.LongTensor``
            A tensor of shape ``(batch_size,)``, describing the re-indexing required to transform
            the outputs back to their original batch order.
        """
        xp = self.xp
        xs = inputs

        batch_lengths = [m.sum() for m in mask]
        indices = argsort_list_descent(batch_lengths)
        indices_array = xp.asarray(indices)

        xs = F.permutate(xs, indices_array, axis=0, inv=False)
        mask = mask[indices_array]
        if hidden_state:
            h, c = hidden_state
            h = F.permutate(h, indices_array, axis=1, inv=False)
            c = F.permutate(c, indices_array, axis=1, inv=False)
            initial_state = (h, c)
            # TODO: test
        else:
            initial_state = None

        batch_lengths = [m.sum() for m in mask]
        module_output, final_states = module(
            xs, batch_lengths=batch_lengths, initial_state=initial_state)

        restoration_indices = indices_array
        return module_output, final_states, restoration_indices

    def _get_initial_states(self,
                            batch_size,
                            num_valid,
                            sorting_indices):
        """
        Returns an initial state for use in an RNN. Additionally, this method handles
        the batch size changing across calls by mutating the state to append initial states
        for new elements in the batch. Finally, it also handles sorting the states
        with respect to the sequence lengths of elements in the batch and removing rows
        which are completely padded. Importantly, this `mutates` the state if the
        current batch size is larger than when it was previously called.

        Parameters
        ----------
        batch_size : ``int``, required.
            The batch size can change size across calls to stateful RNNs, so we need
            to know if we need to expand or shrink the states before returning them.
            Expanded states will be set to zero.
        num_valid : ``int``, required.
            The batch may contain completely padded sequences which get removed before
            the sequence is passed through the encoder. We also need to clip these off
            of the state too.
        sorting_indices ``torch.LongTensor``, required.
            Pytorch RNNs take sequences sorted by length. When we return the states to be
            used for a given call to ``module.forward``, we need the states to match up to
            the sorted sequences, so before returning them, we sort the states using the
            same indices used to sort the sequences.

        Returns
        -------
        This method has a complex return type because it has to deal with the first time it
        is called, when it has no state, and the fact that types of RNN have heterogeneous
        states.

        If it is the first time the module has been called, it returns ``None``, regardless
        of the type of the ``Module``.

        Otherwise, for LSTMs, it returns a tuple of ``torch.Tensors`` with shape
        ``(num_layers, num_valid, state_size)`` and ``(num_layers, num_valid, memory_size)``
        respectively, or for GRUs, it returns a single ``torch.Tensor`` of shape
        ``(num_layers, num_valid, state_size)``.
        """
        # We don't know the state sizes the first time calling forward,
        # so we let the module define what it's initial hidden state looks like.
        if self._states is None:
            return None

        # Otherwise, we have some previous states.
        if batch_size > self._states[0].shape[1]:
            # This batch is larger than the all previous states.
            # If so, resize the states.
            num_states_to_concat = batch_size - self._states[0].shape[1]
            resized_states = []
            # state has shape (num_layers, batch_size, hidden_size)
            for state in self._states:
                # This _must_ be inside the loop because some
                # RNNs have states with different last dimension sizes.
                zeros = self.xp.zeros((state.shape[0],
                                       num_states_to_concat,
                                       state.shape[2]))
                zeros = chainer.Variable(zeros)
                resized_states.append(F.concat([state, zeros], 1))
            self._states = tuple(resized_states)
            correctly_shaped_states = self._states

        elif batch_size < self._states[0].shape[1]:
            # This batch is smaller than the previous one.
            correctly_shaped_states = tuple(
                state[:, :batch_size, :] for state in self._states)
        else:
            correctly_shaped_states = self._states

        # At this point, our states are of shape (num_layers, batch_size, hidden_size).
        # However, the encoder uses sorted sequences and additionally removes elements
        # of the batch which are fully padded. We need the states to match up to these
        # sorted and filtered sequences, so we do that in the next two blocks before
        # returning the state/s.
        if len(self._states) == 1:
            # GRUs only have a single state. This `unpacks` it from the
            # tuple and returns the tensor directly.
            correctly_shaped_state = correctly_shaped_states[0]
            sorted_state = correctly_shaped_state[:, sorting_indices]
            return sorted_state[:, :num_valid, :]
        else:
            # LSTMs have a state tuple of (state, memory).
            sorted_states = [state[:, sorting_indices]
                             for state in correctly_shaped_states]
            return tuple(state[:, :num_valid, :] for state in sorted_states)

    def _update_states(self,
                       final_states,
                       restoration_indices):
        """
        After the RNN has run forward, the states need to be updated.
        This method just sets the state to the updated new state, performing
        several pieces of book-keeping along the way - namely, unsorting the
        states and ensuring that the states of completely padded sequences are
        not updated. Finally, it also detatches the state variable from the
        computational graph, such that the graph can be garbage collected after
        each batch iteration.

        Parameters
        ----------
        final_states : ``RnnStateStorage``, required.
            The hidden states returned as output from the RNN.
        restoration_indices : ``torch.LongTensor``, required.
            The indices that invert the sorting used in ``sort_and_run_forward``
            to order the states with respect to the lengths of the sequences in
            the batch.
        """
        # TODO(Mark): seems weird to sort here, but append zeros in the subclasses.
        # which way around is best?
        # new_unsorted_states = [state.index_select(1, restoration_indices)
        new_unsorted_states = [state[:, restoration_indices]
                               for state in final_states]

        if self._states is None:
            # We don't already have states, so just set the
            # ones we receive to be the current state.
            self._states = tuple([chainer.Variable(state.data)
                                  for state in new_unsorted_states])
        else:
            # Now we've sorted the states back so that they correspond to the original
            # indices, we need to figure out what states we need to update, because if we
            # didn't use a state for a particular row, we want to preserve its state.
            # Thankfully, the rows which are all zero in the state correspond exactly
            # to those which aren't used, so we create masks of shape (new_batch_size,),
            # denoting which states were used in the RNN computation.
            current_state_batch_size = self._states[0].shape[1]
            new_state_batch_size = final_states[0].shape[1]
            # Masks for the unused states of shape (1, new_batch_size, 1)
            used_new_rows_mask = [(state[0, :, :].array.sum(-1) != 0.0)
                                  .reshape((1, new_state_batch_size, 1))
                                  for state in new_unsorted_states]
            new_states = []
            if current_state_batch_size > new_state_batch_size:
                # The new state is smaller than the old one,
                # so just update the indices which we used.
                for old_state, new_state, used_mask in zip(self._states,
                                                           new_unsorted_states,
                                                           used_new_rows_mask):
                    # zero out all rows in the previous state
                    # which _were_ used in the current state.
                    masked_old_state = old_state[:, :new_state_batch_size, :] \
                        * (1 - used_mask)
                    # The old state is larger, so update the relevant parts of it.
                    old_state = old_state.array
                    old_state[:, :new_state_batch_size, :] = \
                        new_state.array + masked_old_state.array
                    # Detatch the Variable.
                    new_states.append(chainer.Variable(old_state))
            else:
                # The states are the same size, so we just have to
                # deal with the possibility that some rows weren't used.
                new_states = []
                for old_state, new_state, used_mask in zip(self._states,
                                                           new_unsorted_states,
                                                           used_new_rows_mask):
                    # zero out all rows which _were_ used in the current state.
                    masked_old_state = old_state * (1 - used_mask)
                    # The old state is larger, so update the relevant parts of it.
                    new_state += masked_old_state
                    # Detatch the Variable.
                    new_states.append(chainer.Variable(new_state.array))

            # It looks like there should be another case handled here - when
            # the current_state_batch_size < new_state_batch_size. However,
            # this never happens, because the states themeselves are mutated
            # by appending zeros when calling _get_inital_states, meaning that
            # the new states are either of equal size, or smaller, in the case
            # that there are some unused elements (zero-length) for the RNN computation.
            self._states = tuple(new_states)

    def reset_states(self):
        self._states = None
