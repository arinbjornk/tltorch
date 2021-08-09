import math
from collections import Iterable
import warnings

import numpy as np
import torch
from torch import nn

import tensorly as tl
tl.set_backend('pytorch')

from .core import TensorizedTensor, _ensure_tuple
from .factorized_tensors import CPTensor, TuckerTensor
from ..utils.parameter_list import FactorList

# Author: Jean Kossaifi
# License: BSD 3 clause

def is_tensorized_shape(shape):
    """Checks if a given shape represents a tensorized tensor."""
    if all(isinstance(s, int) for s in shape):
        return False
    return True

def tensorized_shape_to_shape(tensorized_shape):
    return [s if isinstance(s, int) else np.prod(s) for s in tensorized_shape]

class CPTensorized(TensorizedTensor, CPTensor, name='CP'):
    
    def __init__(self, weights, factors, tensorized_shape, rank=None):
    
        super().__init__(weights, factors, tensorized_shape, rank)

        # Modify only what varies from the Tensor case
        self.shape = tensorized_shape_to_shape(tensorized_shape)
        self.tensorized_shape = tensorized_shape

    @classmethod
    def new(cls, tensorized_shape, rank, **kwargs):
        rank = tl.cp_tensor.validate_cp_rank(tensorized_shape, rank)
        flattened_tensorized_shape = sum([[e] if isinstance(e, int) else list(e) for e in tensorized_shape], [])

        # Register the parameters
        weights = nn.Parameter(torch.Tensor(rank))
        # Avoid the issues with ParameterList
        factors = [nn.Parameter(torch.Tensor(s, rank)) for s in flattened_tensorized_shape]

        return cls(weights, factors, tensorized_shape, rank=rank)

    def __getitem__(self, indices):
        if not isinstance(indices, Iterable):
            indices = [indices]

        output_shape = []
        indexed_factors = []
        factors = self.factors
        weights = self.weights
        
        for (index, shape) in zip(indices, self.tensorized_shape):
            if isinstance(shape, int):
                # We are indexing a "regular" mode
                factor, *factors = factors
                
                if isinstance(index, (np.integer, int)):
                    weights = weights*factor[index, :]
                else:
                    factor = factor[index, :]
                    indexed_factors.append(factor)
                    output_shape.append(factor.shape[0])

            else: 
                # We are indexing a tensorized mode
                
                if index == slice(None) or index == ():
                    # Keeping all indices (:)
                    indexed_factors.extend(factors[:len(shape)])
                    output_shape.append(shape)

                else:
                    if isinstance(index, slice):
                        # Since we've already filtered out :, this is a partial slice
                        # Convert into list
                        max_index = math.prod(shape)
                        index = list(range(*index.indices(max_index)))

                    if isinstance(index, Iterable):
                        output_shape.append(len(index))

                    index = np.unravel_index(index, shape)
                    # Index the whole tensorized shape, resulting in a single factor
                    factor = 1
                    for idx, ff in zip(index, factors[:len(shape)]):
                        factor *= ff[idx, :]

                    if tl.ndim(factor) == 2:
                        indexed_factors.append(factor)
                    else:
                        weights = weights*factor

                factors = factors[len(shape):]
        
        indexed_factors.extend(factors)
        output_shape += [f.shape[0] for f in factors]
        
        if indexed_factors:
            return self.__class__(weights, indexed_factors, tensorized_shape=output_shape)
        return tl.sum(weights)



class TuckerTensorized(TensorizedTensor, TuckerTensor, name='Tucker'):
    
    def __init__(self, core, factors, tensorized_shape, rank=None):
        tensor_shape = sum([(e,) if isinstance(e, int) else tuple(e) for e in tensorized_shape], ())

        super().__init__(core, factors, tensor_shape, rank)

        # Modify only what varies from the Tensor case
        self.shape = tensorized_shape_to_shape(tensorized_shape)
        self.tensorized_shape = tensorized_shape

    @classmethod
    def new(cls, tensorized_shape, rank, n_matrices=(), **kwargs):
        n_matrices = _ensure_tuple(n_matrices)
        tensor_shape = sum([(e,) if isinstance(e, int) else tuple(e) for e in tensorized_shape], ())
        rank = tl.tucker_tensor.validate_tucker_rank(tensor_shape, rank)

        # Register the parameters
        core = nn.Parameter(torch.Tensor(*rank))
        # Avoid the issues with ParameterList
        factors = [nn.Parameter(torch.Tensor(s, r)) for (s, r) in zip(tensor_shape, rank)]

        return cls(core, factors, tensorized_shape, rank=rank)


def validate_block_tt_rank(tensorized_shape, rank):
    ndim = max([1 if isinstance(s, int) else len(s) for s in tensorized_shape])
    factor_shapes = [(s, )*ndim if isinstance(s, int) else s for s in tensorized_shape]
    factor_shapes = list(math.prod(e) for e in zip(*factor_shapes))

    return tl.tt_tensor.validate_tt_rank(factor_shapes, rank)


class BlockTT(TensorizedTensor, name='BlockTT'):
    def __init__(self, factors, tensorized_shape=None, rank=None, batched_dim=None):
        super().__init__()
        self.shape = tensorized_shape_to_shape(tensorized_shape)
        self.tensorized_shape = tensorized_shape
        self.rank = rank
        self.batched_dim = batched_dim
        self.order = len(self.shape)
        self.factors = FactorList(factors)

    @classmethod
    def new(cls, tensorized_shape, rank, batched_dim=(), **kwargs):
        if all(isinstance(s, int) for s in tensorized_shape):
            warnings.warn(f'Given a "flat" shape {tensorized_shape}. '
                          'This will be considered as the shape of a tensorized vector. '
                          'If you just want a 1D tensor, use a regular Tensor-Train. ')
            ndim = 1
            factor_shapes = [tensorized_shape]
            tensorized_shape = (tensorized_shape,)
        else:
            ndim = max([1 if isinstance(s, int) else len(s) for s in tensorized_shape])
            factor_shapes = [(s, )*ndim if isinstance(s, int) else s for s in tensorized_shape]
        
        rank = validate_block_tt_rank(tensorized_shape, rank)
        factor_shapes = [rank[:-1]] + factor_shapes + [rank[1:]]
        factor_shapes = list(zip(*factor_shapes))
        factors = [nn.Parameter(torch.Tensor(*s)) for s in factor_shapes]
        batched_dim = [True if i in batched_dim else False for i in range(ndim)]
        
        return cls(factors, tensorized_shape=tensorized_shape, rank=rank, batched_dim=batched_dim)

    @property
    def decomposition(self):
        return self.factors

    def to_tensor(self):
        ndim = len(self.factors)
        n_modes = self.factors[0].ndim - 2

        order = sum([list(range(i, ndim*n_modes, n_modes)) for i in range(n_modes)], [])

        for i, factor in enumerate(self.factors):
            if not i:
                res = factor
            else:
                res = torch.tensordot(res, factor, ([-1], [0]))

        res = tl.transpose(res.squeeze(0).squeeze(-1), order)
        return tl.reshape(res, self.shape)

    def __torch_function__(self, func, types, args=(), kwargs=None):
        if kwargs is None:
            kwargs = {}

        args = [t.to_tensor() if hasattr(t, 'to_tensor') else t for t in args]
        return func(*args, **kwargs)

    def __getitem__(self, indices):
        factors = self.factors
        if not isinstance(indices, Iterable):
            indices = [indices]

        if len(indices) < self.ndim:
            indices = list(indices)
            indices.extend([slice(None)]*(self.ndim - len(indices)))

        output_shape = []
        indexed_factors = []
        ndim = len(self.factors)
        indexed_ndim = len(indices)

        contract_factors = False # If True, the result is dense, we need to form the full result
        contraction_op = [] # Whether the operation is batched or not
        eq_in1 = 'a' # Previously contracted factors (rank_0, dim_0, ..., dim_N, rank_k)
        eq_in2 = 'b' # Current factor (rank_k, dim_0', ..., dim_N', rank_{k+1})
        eq_out = 'a' # Output contracted factor (rank_0, dim_0", ..., dim_N", rank_{k_1})
        # where either:
        #     i. dim_k" = dim_k' = dim_k (contraction_op='b' for batched)
        # or ii. dim_k" = dim_k' x dim_k (contraction_op='m' for multiply)
        
        idx = ord('d') # Current character we can use for contraction
        
        pad = (slice(None), ) # index previous dimensions with [:], to avoid using .take(dim=k)
        add_pad = False       # whether to increment the padding post indexing
        
        for (index, shape) in zip(indices, self.tensorized_shape):
            if isinstance(shape, int):
                # We are indexing a "regular" mode, not a tensorized one            
                if not isinstance(index, (np.integer, int)):
                    if isinstance(index, slice):
                        index = list(range(*index.indices(shape)))

                    output_shape.append(len(index))
                    add_pad = True
                    contraction_op += 'b' # batched
                    eq_in1 += chr(idx); eq_in2 += chr(idx); eq_out += chr(idx)
                    idx += 1
                # else: we've essentially removed a mode of each factor
                index = [index]*ndim
            else: 
                # We are indexing a tensorized mode

                if index == slice(None) or index == ():
                    # Keeping all indices (:)
                    output_shape.append(shape)

                    eq_in1 += chr(idx)
                    eq_in2 += chr(idx+1)
                    eq_out += chr(idx) + chr(idx+1)
                    idx += 2
                    add_pad = True
                    index = [index]*ndim
                    contraction_op += 'm' # multiply
                else:
                    contract_factors = True

                    if isinstance(index, slice):
                        # Since we've already filtered out :, this is a partial slice
                        # Convert into list
                        max_index = math.prod(shape)
                        index = list(range(*index.indices(max_index)))

                    if isinstance(index, Iterable):
                        output_shape.append(len(index))
                        contraction_op += 'b' # multiply
                        eq_in1 += chr(idx)
                        eq_in2 += chr(idx)
                        eq_out += chr(idx)
                        idx += 1
                        add_pad = True

                    index = np.unravel_index(index, shape)

            # Index the whole tensorized shape, resulting in a single factor
            factors = [ff[pad + (idx,)] for (ff, idx) in zip(factors, index)]# + factors[indexed_ndim:]
            if add_pad:
                pad += (slice(None), )
                add_pad = False
                
#         output_shape.extend(self.tensorized_shape[indexed_ndim:])

        if contract_factors:
            eq_in2 += 'c'
            eq_in1 += 'b'
            eq_out += 'c'
            eq = eq_in1 + ',' + eq_in2 + '->' + eq_out
            for i, factor in enumerate(factors):
                if not i:
                    res = factor
                else:
                    out_shape = list(res.shape)
                    for j, s in enumerate(factor.shape[1:-1]):
                        if contraction_op[j] == 'm':
                            out_shape[j+1] *= s
                    out_shape[-1] = factor.shape[-1] # Last rank
                    res = tl.reshape(tl.einsum(eq, res, factor), out_shape)
            return res.squeeze()
        else:
            return self.__class__(factors, output_shape, self.rank)