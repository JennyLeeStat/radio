"""Contains class CTImagesMaskedBatch for storing masked Ct-scans."""

from concurrent.futures import ThreadPoolExecutor
from binascii import hexlify
# from itertools import chain
import os
import sys
import shutil
import blosc
import numpy as np
from numba import jit
from numba import njit
import SimpleITK as sitk
from .ct_batch import CTImagesBatch
from .mask import make_mask_patient
from .resize import resize_patient_numba
from .dataset_import import action
from .dataset_import import inbatch_parallel
from .dataset_import import any_action_failed

@jit('float64[:, :, :](float64[:, :, :], int32[:, :], int32[:])', nogil=True)
def get_nodules_jit(data, positions, size):
    """Fetch nodules from array by array of starting positions.

    This numberized function takes source array with data of shape (n, k, l)
    represented by 3d numpy array with BatchCt data,
    ndarray(p, 3) with starting indices of nodules where p is number
    of nodules and size of type ndarray(3, ) which contains
    sizes of nodules along each axis. The output is 3d ndarray with nodules
    put in CTImagesBatch-compatible skyscraper structure.

    *Note that dtypes of positions and size arrays must be the same.

    Args:
    - data: CTImagesBatch skyscraper represented by 3d numpy array;
    - positions: ndarray(l, 3) of int32 containing
      nodules' starting indices along [zyx]-axis
      accordingly in ndarray data;
    - size: ndarray(3,) of int32 containing
      nodules' sizes along each axis;
    """
    out_arr = np.zeros((positions.shape[0], size[0],
                        size[1], size[2]), dtype=data.dtype)

    n_positions = positions.shape[0]
    for i in range(n_positions):
        out_arr[i, :, :, :] = data[positions[i, 0]: positions[i, 0] + size[0],
                                   positions[i, 1]: positions[i, 1] + size[1],
                                   positions[i, 2]: positions[i, 2] + size[2]]

    return out_arr.reshape(n_positions * size[0], size[1], size[2])


class CTImagesMaskedBatch(CTImagesBatch):
    """Class for storing masked batch of ct-scans.

    In addition to batch itself, stores mask in
    self.mask as ndarray, origin and spacing dictionaries
    and list with information about nodules in batch.

    new attrs:
        1. mask: ndarray of masks
        2. spacing: dict with keys = self.indices
            stores distances between pixels in mm for patients
            order is x, y, z
        3. origin: dict with keys = self.indices
            stores world coords of [0, 0, 0]-pixel of data for
            all patients
        4. nodules_info: list with information about nodule; each nodule
            represented by instance of Nodule class

    Important methods:
        1. load_mask(self, nodules_df, num_threads=8)
            function for
            loading masks from dataframe with nodules
            multithreading is supported
        2. resize(self, num_x_new=256, num_y_new=256,
                  num_slices_new=128, order=3, num_threads=8)
            transform shape of all patients to
            (num_slices_new, num_y_new, num_x_new)
            if masks are loaded, they are are also resized

        *Note: spacing, origin are recalculated when resize is executed
            As a result, load_mask can be also executed after resize
    """

    @staticmethod
    def make_indices(size: 'int'):
        """Generate list of batch indices of given size.

        Take number of indices as input parameter size and
        generates list of random indices of length size.

        Args:
        - size: size of list with indices;
        """
        random_data = np.random.uniform(0, 1, size=(size, 10)) * 123456789
        indices = [hexlify(random_data[i, :])[:8].decode("utf-8")
                   for i in range(size)]
        return indices

    @staticmethod
    def dump_blosc(data, index, path):
        """Dump data on hard disk in blosc format.

        Save data on hard drive in file with path
        os.path.join(path, str(index)).
        """
        full_path = os.path.join(path, index)
        packed = blosc.pack_array(data, cname='zstd', clevel=1)
        if os.path.exists(full_path):
            shutil.rmtree(full_path)
        os.makedirs(full_path)
        with open(os.path.join(full_path, 'data.blk'), mode='wb') as dump_file:
            dump_file.write(packed)

    def __init__(self, index):
        """Initialization of CTImagesMaskedBatch.

        Initialize CTImagesMaskedBatch with index.
        """
        super().__init__(index)
        self.mask = None
        self.nodules = None
        self.nodules_pat_pos = None
        self.spacing = np.zeros((len(self.index), 3))
        self.origin = np.zeros((len(self.index), 3))

    @action
    def load(self, src=None, fmt='dicom', bounds=None,
             origin=None, spacing=None, nodules=None):  # pylint: disable=arguments-differ
        """Load data in masked batch of patients.

        Args:
        - src: source array with skyscraper, needed if fmt is 'ndarray';
        - fmt: type of source data; possible values are 'raw' and 'ndarray';
        Returns:
        - self;

        Examples:
        >>> index = FilesIndex(path="/some/path/*.mhd, no_ext=True")
        >>> batch = CTImagesMaskedBatch(index)
        >>> batch.load(fmt='raw')

        >>> batch.load(src=source_array, fmt='ndarray', bounds=bounds,
        ...            origin=origin_dict, spacing=spacing_dict)
        """
        if fmt == 'raw':
            self._load_raw()
            self.mask = np.zeros_like(self.data)
        elif fmt in ['dicom', 'blosc']:
            raise NotImplementedError("This load format option " +
                                      "is not implemented for masked batch")
        elif fmt == 'ndarray':
            self._data = src
            self._bounds = bounds
            self.origin = origin
            self.spacing = spacing
            self.nodules = nodules
        else:
            raise TypeError("Incorrect type of batch source")
        return self

    @inbatch_parallel(init='indices', post='_post_default', target='threads')
    def _load_raw(self, patient_id, *args, **kwargs):
        """Read, prepare and put 3d-scans in array from raw(mhd).

        This method reads 3d-scans from mhd format
        in CTImagesMaskedBatch object. This method additionaly
        initializes origin and spacing attributes.

        Args:
        - patient_id: index of patient from batch, whose scans need to
        be put in stack(skyscraper);

        Return :
        - ndarray(Nz, Ny, Nx) patient's data array;
        """
        raw_data = sitk.ReadImage(self.index.get_fullpath(patient_id))
        patient_pos = self.index.get_pos(patient_id)
        self.origin[patient_pos, :] = np.array(raw_data.GetOrigin())[::-1]
        self.spacing[patient_pos, :] = np.array(raw_data.GetSpacing())[::-1]
        return sitk.GetArrayFromImage(raw_data)

    def get_mask(self, index):
        """Get view on patient data's mask.

        This method takes position of patient in self or his index
        and returns view on patient data's mask.

        Args:
        - index: can be either position of patient in self._data
        or index from self.index;

        Return:
        - ndarray(Nz, Ny, Nz): view on patient data's mask array;
        """
        if isinstance(index, int):
            if index < self._bounds.shape[0] - 1 and index >= 0:
                pos = index
            else:
                raise IndexError("Index is out of range")
        else:
            pos = self.index.get_pos(index)

        lower = self._bounds[pos]
        upper = self._bounds[pos + 1]
        return self.mask[lower: upper, :, :]

    @action
    def fetch_nodules_info(self, nodules_df, update=False):
        """Get nodules in 3d ndarray.

        This method fetch info about all nodules in batch
        and put them in 2d numpy array.
        """
        if self.nodules is not None and not update:
            return self
        nodules_df = nodules_df.set_index('seriesuid')

        unique_indices = nodules_df.index.unique()
        inter_index = np.intersect1d(unique_indices, self.indices)
        # inter_index = list(set(self.indices) & set(unique_indices))
        nodules_df = nodules_df.loc[inter_index,
                                    ["coordZ", "coordY",
                                     "coordX", "diameter_mm"]]

        n_nodules = nodules_df.shape[0]
        nod_pat_pos = np.zeros(n_nodules, dtype=np.int32)
        spacing_arr = np.zeros((n_nodules, 3))
        origin_arr = np.zeros((n_nodules, 3))
        center_arr = np.zeros((n_nodules, 3))
        size_arr = np.zeros((n_nodules, 3))
        bias_arr = np.zeros((n_nodules, 3))
        img_size_arr = np.zeros((n_nodules, 3))
        counter = 0

        for pat_id, coordz, coordy, coordx, diam in nodules_df.itertuples():
            pat_pos = self.index.get_pos(pat_id)
            nod_pat_pos[counter] = pat_pos

            img_size_arr[counter, :] = np.array(self[pat_id].shape)
            center_arr[counter, :] = np.array([coordz, coordy, coordx])
            size_arr[counter, :] = np.array([diam] * 3)
            counter += 1

        bias_arr = np.stack([self._bounds[nod_pat_pos],
                             np.zeros(n_nodules),
                             np.zeros(n_nodules)]).T

        spacing_arr = self.spacing[nod_pat_pos, :]
        origin_arr = self.origin[nod_pat_pos, :]
        self.nodules = np.rec.array([bias_arr, origin_arr,
                                     spacing_arr, center_arr,
                                     size_arr, img_size_arr],
                                    names=['bias', 'origin', 'spacing',
                                           'center', 'size', 'img_size'])
        self.nodules_pat_pos = nod_pat_pos
        return self

    def _shift_out_of_bounds(self, size):
        """Fetch start pixel coordinates of all nodules.

        This method returns start pixel coordinates of all nodules
        in batch. Note that all nodules are considered to have the
        fixed size defined by argument size: if nodule is out of
        patient's 3d image bounds than it's center is shifted.

        Args:
        - size: list, tuple of numpy array of length 3 with pixel
        size of nodules.
        """
        size = np.array(size, dtype=np.int)
        center_pix = np.abs(self.nodules.center - self.nodules.origin) / self.nodules.spacing
        start_pix = (np.rint(center_pix) - np.rint(size / 2))
        end_pix = start_pix + size
        bias_upper = np.where(end_pix > self.nodules.img_size,
                              end_pix - self.nodules.img_size, 0)

        start_pix -= bias_upper
        end_pix -= bias_upper

        bias_lower = np.where(start_pix < 0, -start_pix, 0)

        start_pix += bias_lower
        end_pix += bias_lower

        return (start_pix + self.nodules.bias).astype(np.int)

    @action
    def create_mask(self):
        """Load mask data for using nodule's info.

        Load mask into self.mask using info in attribute self.nodules_info.
        *Note: nodules info must be loaded before the call of this method.
        """
        if self.nodules is None:
            raise AttributeError("Info about nodules location must " +
                                 "be loaded before calling this method")

        center_pix = np.rint(np.abs(self.nodules.center - self.nodules.origin) /
                             self.nodules.spacing)
        size_pix = np.rint(self.nodules.size / self.nodules.spacing).astype(np.int)
        start_pix = (center_pix - np.rint(size_pix / 2)).astype(np.int)
        for patient_id in self.indices:
            ndarray_mask = (self.nodules_pat_pos == self.index.get_pos(patient_id))
            if np.any(ndarray_mask):
                make_mask_patient(self.get_mask(patient_id),
                                  start_pix[ndarray_mask, :],
                                  size_pix[ndarray_mask, :])

        return self

    def sample_random_nodules(self, n_nodules: 'int',
                              nodule_size: 'ndarray(3, )') -> "ndarray(l, 3)":
        """Sample random nodules from CTImagesBatchMasked skyscraper.

        Samples random n_nodules' lower_bounds coordinates
        and stack obtained data into ndarray(l, 3) then returns it.
        First dimension of that array is just an index of sampled
        nodules while second points out pixels of start of nodules
        in BatchCt skyscraper. Each nodule have shape
        defined by parameter size. If size of patients' data along
        z-axis is not the same for different patients than
        NotImplementedError will be raised.

        Args:
        - n_nodules: number of random nodules to sample from BatchCt data;
        - nodule_size: ndarray(3, ) nodule size in number of pixels;

        return
        - ndarray(l, 3) of int that contains information
        about starting positions
        of sampled nodules in BatchCt skyscraper along each axis.
        First dimension is used to index nodules
        while the second one refers to various axes.

        *Note: [zyx]-ordering is used;
        """
        all_indices = np.arange(len(self.indices))
        sampled_indices = np.random.choice(all_indices,
                                           n_nodules, replace=True)

        shape_z = (self._bounds[sampled_indices] -
                   self._bounds[sampled_indices + 1])
        shape_z = shape_z.reshape(-1, 1)

        offset = np.vstack([np.asarray(self._bounds[sampled_indices]),
                            np.zeros(n_nodules), np.zeros(n_nodules)]).T

        shapes_yx = np.tile([self.data.shape[1],
                             self.data.shape[2]], n_nodules)

        shapes_yx = shapes_yx.reshape(-1, 2)

        data_shape = np.concatenate([shape_z, shapes_yx], axis=1)
        samples = np.random.rand(n_nodules, 3) * (data_shape - nodule_size)
        return samples + offset

    @action
    def sample_nodules(self, batch_size,
                       nodule_size, share=0.8) -> 'CTImagesBatchMasked':
        """Fetch random cancer and non-cancer nodules from batch.

        Fetch nodules from CTImagesBatchMasked into ndarray(l, m, k).

        Args:
        - nodules_df: dataframe of csv file with information
        about nodules location;
        - batch_size: number of nodules in the output batch. Must be int;
        - nodule_size: size of nodule along axes.
        Must be list, tuple or nsystem pathdarray(3, ) of integer type;
        (Note: using zyx ordering)
        - share: share of cancer nodules in the batch.
        If source CTImagesBatch contains less cancer
        nodules than needed random nodules will be taken;
        """
        if self.nodules is None:
            raise AttributeError("Info about nodules location must " +
                                 "be loaded before calling this method")

        nodule_size = np.asarray(nodule_size, dtype=np.int32)

        n_nodules = self.nodules_pat_pos.shape[0]

        cancer_n = int(share * batch_size)
        cancer_n = n_nodules if cancer_n > n_nodules else cancer_n
        if n_nodules == 0:
            cancer_nodules = np.zeros((0, 3))
        else:
            sample_indices = np.random.choice(np.arange(n_nodules),
                                              size=cancer_n, replace=False)
            cancer_nodules = self._shift_out_of_bounds(nodule_size)
            cancer_nodules = cancer_nodules[sample_indices, :]

        random_nodules = self.sample_random_nodules(batch_size - cancer_n,
                                                    nodule_size)

        nodules_indices = np.vstack([cancer_nodules,
                                     random_nodules]).astype('int32')  # pylint: disable=no-member

        data = get_nodules_jit(self.data, nodules_indices, nodule_size)
        mask = get_nodules_jit(self.mask, nodules_indices, nodule_size)
        bounds = np.arange(data.shape[0] + 1) * nodule_size[0]

        nodules_batch = CTImagesMaskedBatch(self.make_indices(batch_size))
        nodules_batch.load(src=data, fmt='ndarray', bounds=bounds)
        nodules_batch.mask = mask
        nodules_batch.origin = None
        nodules_batch.spacing = None
        return nodules_batch

    @action
    def dump(self, dst, fmt='blosc', dtype='source'):
        """Dump patients data and mask(optional) on disc.

        Dump on specified path and format
        create folder corresponding to each patient
        *Note: this method is decorated with @history and @action.
        If mask_dst in not None than dump mask too.

        Example:
        # initialize batch and load data
        >>> ind = ['1ae34g90', '3hf82s76', '2ds38d04']
        >>> batch = BatchCt(ind)
        >>> batch.load(...)
        >>> batch.dump('./data/blosc_preprocessed', dtype='source')

        # the command above creates files
        # ./data/blosc_preprocessed/1ae34g90/data.blk
        # ./data/blosc_preprocessed/3hf82s76/data.blk
        # ./data/blosc_preprocessed/2ds38d04/data.blk
        """
        dtype_values = ['source', 'mask']
        if isinstance(dtype, (tuple, list)):
            if any(dt not in dtype_values for dt in dtype):
                raise ValueError("Argument dtype must be list or tuple" +
                                 "containing 'source' or 'mask'")
            if len(dtype) != len(dst):
                raise ValueError("Arguments dtype and dst must have " +
                                 "the same length if having " +
                                 "type list or tuple")

        elif not(isinstance(dtype, str) and isinstance(dst, str)):
            raise ValueError("Arguments dtype and dst must " +
                             "have the same type")

        for patient_id in self.indices:
            patient_pos = self.index.get_pos(patient_id)
            lower = self._bounds[patient_pos]
            upper = self._bounds[patient_pos + 1]

            for dump_type, dump_path in zip(dtype, dst):
                if dump_type == 'source':
                    self.dump_blosc(self.data[lower: upper, :, :],
                                    patient_id, dump_path)
                elif dump_type == 'mask':
                    self.dump_blosc(self.mask[lower: upper, :, :],
                                    patient_id, dump_path)
        return self

    def get_axial_slice(self, patient_pos, height):
        """Get tuple of slices (data slice, mask slice).

        Args:
            patient_pos: patient position in the batch
            height: height, take slices with number
                int(0.7 * number of slices for patient) from
                patient's scan and mask
        """
        margin = int(slice_height * self[patient_pos].shape[0])

        patch = (self.get_image(patient_pos)[margin, :, :],
                 self.get_mask(patient_pos)[margin:, :, :])
        return patch

    def _rescale_spacing(self, new_shape):
        """Rescale spacing during resize.

        During resize action it is neccessary to update patient's
        current spacing cause it used for mask creation
        and nodules extraction.

        Args:
        - new_shape: list, tuple or ndarray(3, ) that represents
        new_shape of patient's scans;
        Returns:
        - self;
        """
        if self.spacing is None:
            return self

        new_shape = np.asarray(new_shape)
        for patient_id in self.indices:
            old_shape = np.asarray(self.get_image(patient_id).shape)
            self.spacing[self.index.get_pos(patient_id)] *= (old_shape / new_shape)

        if self.nodules is not None:
            n_nodules = self.nodules_pat_pos.shape[0]
            self.nodules.spacing = self.spacing[self.nodules_pat_pos, :]
            self.nodules.img_size = np.tile(new_shape,
                                            n_nodules).reshape(n_nodules, 3)
            self.nodules.bias = np.zeros((n_nodules, 3))
            self.nodules.bias[:, 0] = (np.arange(len(self.index)) *
                                       new_shape[0])[self.nodules_pat_pos]
        return self

    def _init_rebuild(self, **kwargs):
        """Args-fetcher for resize parallelization.

        args-fetcher for parallelization using decorator
            can be used when batch-data is rebuild from scratch
        if shape is supplied as one of the args
            assumes that data should be resizd
        """
        if 'shape' not in kwargs:
            raise TypeError("Output shape must be" +
                            "specified in argument shape!")
        self._rescale_spacing(new_shape=kwargs['shape'])
        return super()._init_rebuild(**kwargs)


    @action
    @inbatch_parallel(init='_init_rebuild', post='_post_rebuild', target='nogil')
    def resize(self, shape=(256, 256, 128), order=3, *args, **kwargs):    # pylint: disable=unused-argument, no-self-use
        """
        performs resize (change of shape) of each CT-scan in the batch.
            When called from Batch, changes Batch
            returns self
        args:
            shape: needed shape after resize in order x, y, z
                *note that the order of axes in data is z, y, x
                 that is, new patient shape = (shape[2], shape[1], shape[0])
            n_workers: number of threads used (degree of parallelism)
                *note: available in the result of decoration of the function
                above
            order: the order of interpolation (<= 5)
                large value improves precision, but slows down the computaion
        example:
            shape = (256, 256, 128)
            Batch = Batch.resize(shape=shape, n_workers=20, order=2)
        """
        return resize_patient_numba


    def _post_rebuild(self, all_outputs, new_batch=False, **kwargs):
        """Post-function for resize parallelization.

        gatherer of outputs from different workers for
            ops, requiring complete rebuild of batch._data
        args:
            new_batch: if True, returns new batch with data
                agregated from workers_ouputs
        """
        # TODO: process errors
        batch = super()._post_rebuild(all_outputs, new_batch, **kwargs)
        batch.origin = self.origin
        batch.spacing = self.spacing
        batch.mask = np.zeros_like(batch.data)
        if self.nodules is not None:
            batch.nodules = self.nodules
            batch.nodules_pat_pos = self.nodules_pat_pos
            batch.create_mask()
        return batch

    def _init_create_mask(self, *kwargs):
        center_pix = np.rint(np.abs(self.nodules.center -
                                    self.nodules.origin) / self.nodules.spacing)
        size_pix = np.rint(self.nodules.size /
                           self.nodules.spacing).astype(np.int)
        start_pix = (center_pix - np.rint(size_pix / 2)).astype(np.int)

        args_list = []
        for patient_pos, patient_id in enumerate(self.indices):
            ndarray_mask = (self.nodules_pat_pos == self.index.get_pos(patient_id))
            ndarray_mask = (self.nodules_pat_pos == self.index.get_pos(patient_id))
            if np.any(ndarray_mask):
                args_list.append({'patient_mask': patient_id,
                                  'start': start_pix[ndarray_mask, :],
                                  'size': size_pix[ndarray_mask, :]})
        return args_list

    def _post_create_mask(self, list_of_res, **kwargs):
        if any_action_failed(list_of_res):
            assert "Some actions failed during threading"
        return self


    @action
    @inbatch_parallel(init='_init_create_mask',
                      post='_post_create_mask',
                      target='threads')
    def create_mask_parallel(self, patient_id, start, size):
        return make_mask_patient(self.get_mask(patient_id), start, size)

    # def _init_images_mask(self, **kwargs):
    #     """Parallelization initializer for both mask and images data.
    #
    #     Args fetcher for parallelization using decorator. Fetch arguments
    #     both for masks and images.
    #     """
    #     result = chain((self.get_image(patient_id) for patient_id in self.indices),
    #                    (self.get_mask(patient_id) for patient_id in self.indices))
    #     return result
    #
    # def _post_images_mask(self, all_outputs, update=True, new_batch=False, **kwargs):
    #     """Gather outputs of differecnt workers into batch.
    #
    #     This protected method assumes that first half of all_outputs list
    #     all_outputs[:len(self.index)]
    #     contains output data for images and
    #     the second half all_outputs[:len(self.index)]
    #     contains output data for  masks.
    #     """
    #     if any_action_failed(all_outputs):
    #         raise ValueError("Failed while parallelizing")
    #
    #     new_bounds = np.cumsum([patient_shape[0] for _, patient_shape
    #                             in [[0, (0, )]] + all_outputs[:len(self.index)]])
    #
    #     new_images = np.concatenate(all_outputs[:len(self.index)], axis=0)
    #     new_masks = np.concatenate(all_outputs[len(self.index):
    #                                            2 * len(self.index)], axis=0)
    #
    #     if new_batch:
    #         batch_res = type(self)(self.index)
    #         batch_res.load(src=new_images, bounds=new_bounds)
    #         batch_res.mask =new_masks
    #         batch_res.nodules = self.nodules
    #         batch_res.nodules_pat_pos = self.nodules_pat_pos
    #         return batch_res
    #     else:
    #         self._data = new_data
    #         self.mask = new_masks
    #         self._bounds = new_bounds
    #         return self
    #
    #
    # @action
    # @inbatch_parallel(init='_init_images_mask',
    #                   post='_post_images_mask',
    #                   target='nogil', new_batch=False)
    # def make_xip(self, step=2, depth=10, func='max', projection='axial', *args, **kwargs):    # pylint: disable=unused-argument, no-self-use
    #     """
    #     This function takes 3d picture represented by np.ndarray image,
    #     start position for 0-axis index, stop position for 0-axis index,
    #     step parameter which represents the step across 0-axis and, finally,
    #     depth parameter which is associated with the depth of slices across
    #     0-axis made on each step for computing MEAN, MAX, MIN
    #     depending on func argument.
    #     Possible values for func are 'max', 'min' and 'avg'.
    #     Notice that 0-axis in this annotation is defined in accordance with
    #     projection argument which may take the following values: 'axial',
    #     'coroanal', 'sagital'.
    #     Suppose that input 3d-picture has axis associations [z, x, y], then
    #     axial projection doesn't change the order of axis and 0-axis will
    #     be correspond to 0-axis of the input array.
    #     However in case of 'coronal' and 'sagital' projections the source tensor
    #     axises will be transposed as [x, z, y] and [y, z, x]
    #     for 'coronal' and 'sagital' projections correspondingly.
    #     """
    #     return xip_fn_numba(func, projection, step, depth)
