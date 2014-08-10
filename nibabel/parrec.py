# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Humble attempt to read images in PAR/REC format.

This is yet another MRI image format generated by Philips scanners. It is an
ASCII header (PAR) plus a binary blob (REC).

This implementation aims to read version 4.2 of this format. Other versions
could probably be supported, but the author is lacking samples of them.

###############
PAR file format
###############

The PAR format appears to have two sections:

General information
###################

This is a set of lines each giving one key : value pair, examples::

    .    EPI factor        <0,1=no EPI>     :   39
    .    Dynamic scan      <0=no 1=yes> ?   :   1
    .    Diffusion         <0=no 1=yes> ?   :   0

(from nibabe/tests/data/phantom_EPI_asc_CLEAR_2_1.PAR)

Image information
#################

There is a ``#`` prefixed list of fields under the heading "IMAGE INFORMATION
DEFINITION".  From the same file, here is the start of this list::

    # === IMAGE INFORMATION DEFINITION =============================================
    #  The rest of this file contains ONE line per image, this line contains the following information:
    #
    #  slice number                             (integer)
    #  echo number                              (integer)
    #  dynamic scan number                      (integer)

There follows a space separated table with values for these fields, each row
containing all the named values. Here's the first few lines from the example
file above::

    # === IMAGE INFORMATION ==========================================================
    #  sl ec  dyn ph ty    idx pix scan% rec size                (re)scale              window        angulation              offcentre        thick   gap   info      spacing     echo     dtime   ttime    diff  avg  flip    freq   RR-int  turbo delay b grad cont anis         diffusion       L.ty

    1   1    1  1 0 2     0  16    62   64   64     0.00000   1.29035 4.28404e-003  1070  1860 -13.26  -0.00  -0.00    2.51   -0.81   -8.69  6.000  2.000 0 1 0 2  3.750  3.750  30.00    0.00     0.00    0.00   0   90.00     0    0    0    39   0.0  1   1    8    0   0.000    0.000    0.000  1
    2   1    1  1 0 2     1  16    62   64   64     0.00000   1.29035 4.28404e-003  1122  1951 -13.26  -0.00  -0.00    2.51    6.98  -10.53  6.000  2.000 0 1 0 2  3.750  3.750  30.00    0.00     0.00    0.00   0   90.00     0    0    0    39   0.0  1   1    8    0   0.000    0.000    0.000  1
    3   1    1  1 0 2     2  16    62   64   64     0.00000   1.29035 4.28404e-003  1137  1977 -13.26  -0.00  -0.00    2.51   14.77  -12.36  6.000  2.000 0 1 0 2  3.750  3.750  30.00    0.00     0.00    0.00   0   90.00     0    0    0    39   0.0  1   1    8    0   0.000    0.000    0.000  1

###########
Orientation
###########

PAR files refer to orientations "ap", "fh" and "rl".

Nibabel's required affine output axes are RAS (left to Right, posterior to
Anterior, inferior to Superior). The correspondence of the PAR file's axes to
RAS axes is:

* ap = anterior -> posterior = negative A in RAS
* fh = foot -> head = S in RAS
* rl = right -> left = negative R in RAS

The orientation of the PAR file axes corresponds to DICOM's LPS coordinate
system (right to Left, anterior to Posterior, inferior to Superior), but in a
different order.

We call the PAR file's axis system "PSL" (Posterior, Superior, Left)
"""
from __future__ import print_function, division

import warnings
import numpy as np
from copy import deepcopy

from .externals.six import binary_type
from .py3k import asbytes

from .spatialimages import SpatialImage, Header
from .eulerangles import euler2mat
from .volumeutils import Recoder, array_from_file, apply_read_scaling
from .arrayproxy import ArrayProxy
from .affines import from_matvec, dot_reduce

# PSL to RAS affine
PSL_TO_RAS = np.array([[0, 0, -1, 0], # L -> R
                       [-1, 0, 0, 0], # P -> A
                       [0, 1, 0, 0],  # S -> S
                       [0, 0, 0, 1]])

# Acquisition (tra/sag/cor) to PSL axes
# These come from looking at transverse, sagittal, coronal datasets where we
# can see the LR, PA, SI orientation of the slice axes from the scanned object
ACQ_TO_PSL = dict(
    transverse = np.array([[  0,  1,  0, 0], # P
                           [  0,  0,  1, 0], # S
                           [  1,  0,  0, 0], # L
                           [  0,  0,  0, 1]]),
    sagittal = np.diag([1, -1, -1, 1]),
    coronal = np.array([[  0,  0,  1, 0], # P
                        [  0, -1,  0, 0], # S
                        [  1,  0,  0, 0], # L
                        [  0,  0,  0, 1]])
)
# PAR header versions we claim to understand
supported_versions = ['V4.2']

# General information dict definitions
# assign props to PAR header entries
# values are: (shortname[, dtype[, shape]])
_hdr_key_dict = {
    'Patient name': ('patient_name',),
    'Examination name': ('exam_name',),
    'Protocol name': ('protocol_name',),
    'Examination date/time': ('exam_date',),
    'Series Type': ('series_type',),
    'Acquisition nr': ('acq_nr', int),
    'Reconstruction nr': ('recon_nr', int),
    'Scan Duration [sec]': ('scan_duration', float),
    'Max. number of cardiac phases': ('max_cardiac_phases', int),
    'Max. number of echoes': ('max_echoes', int),
    'Max. number of slices/locations': ('max_slices', int),
    'Max. number of dynamics': ('max_dynamics', int),
    'Max. number of mixes': ('max_mixes', int),
    'Patient position': ('patient_position',),
    'Preparation direction': ('prep_direction',),
    'Technique': ('tech',),
    'Scan resolution  (x, y)': ('scan_resolution', int, (2,)),
    'Scan mode': ('san_mode',),
    'Repetition time [ms]': ('repetition_time', float),
    'FOV (ap,fh,rl) [mm]': ('fov', float, (3,)),
    'Water Fat shift [pixels]': ('water_fat_shift', float),
    'Angulation midslice(ap,fh,rl)[degr]': ('angulation', float, (3,)),
    'Off Centre midslice(ap,fh,rl) [mm]': ('off_center', float, (3,)),
    'Flow compensation <0=no 1=yes> ?': ('flow_compensation', int),
    'Presaturation     <0=no 1=yes> ?': ('presaturation', int),
    'Phase encoding velocity [cm/sec]': ('phase_enc_velocity', float, (3,)),
    'MTC               <0=no 1=yes> ?': ('mtc', int),
    'SPIR              <0=no 1=yes> ?': ('spir', int),
    'EPI factor        <0,1=no EPI>': ('epi_factor', int),
    'Dynamic scan      <0=no 1=yes> ?': ('dyn_scan', int),
    'Diffusion         <0=no 1=yes> ?': ('diffusion', int),
    'Diffusion echo time [ms]': ('diffusion_echo_time', float),
    'Max. number of diffusion values': ('max_diffusion_values', int),
    'Max. number of gradient orients': ('max_gradient_orient', int),
    'Number of label types   <0=no ASL>': ('nr_label_types', int),
    }

# Image information as coded into a numpy structured array
# header items order per image definition line
image_def_dtd = [
    ('slice number', int),
    ('echo number', int,),
    ('dynamic scan number', int,),
    ('cardiac phase number', int,),
    ('image_type_mr', int,),
    ('scanning sequence', int,),
    ('index in REC file', int,),
    ('image pixel size', int,),
    ('scan percentage', int,),
    ('recon resolution', int, (2,)),
    ('rescale intercept', float),
    ('rescale slope', float),
    ('scale slope', float),
    ('window center', int,),
    ('window width', int,),
    ('image angulation', float, (3,)),
    ('image offcentre', float, (3,)),
    ('slice thickness', float),
    ('slice gap', float),
    ('image_display_orientation', int,),
    ('slice orientation', int,),
    ('fmri_status_indication', int,),
    ('image_type_ed_es', int,),
    ('pixel spacing', float, (2,)),
    ('echo_time', float),
    ('dyn_scan_begin_time', float),
    ('trigger_time', float),
    ('diffusion_b_factor', float),
    ('number of averages', int,),
    ('image_flip_angle', float),
    ('cardiac frequency', int,),
    ('minimum RR-interval', int,),
    ('maximum RR-interval', int,), 
    ('TURBO factor', int,),
    ('Inversion delay', float),
    ('diffusion b value number', int,),    # (imagekey!)
    ('gradient orientation number', int,), # (imagekey!)
    ('contrast type', 'S30'),              # XXX might be too short?
    ('diffusion anisotropy type', 'S30'),  # XXX might be too short?
    ('diffusion', float, (3,)),
    ('label type', int,),                  # (imagekey!)
    ]
image_def_dtype = np.dtype(image_def_dtd)

# slice orientation codes
slice_orientation_codes = Recoder((# code, label
    (1, 'transverse'),
    (2, 'sagittal'),
    (3, 'coronal')), fields=('code', 'label'))


class PARRECError(Exception):
    """Exception for PAR/REC format related problems.

    To be raised whenever PAR/REC is not happy, or we are not happy with
    PAR/REC.
    """
    pass


def parse_PAR_header(fobj):
    """Parse a PAR header and aggregate all information into useful containers.

    Parameters
    ----------
    fobj : file-object
        The PAR header file object.

    Returns
    -------
    general_info : dict
        Contains all "General Information" from the header file
    image_info : ndarray
        Structured array with fields giving all "Image information" in the
        header
    """
    # containers for relevant header lines
    general_info = {}
    image_info = []
    version = None

    # single pass through the header
    for line in fobj:
        # no junk
        line = line.strip()
        if line.startswith('#'):
            # try to get the header version
            if line.count('image export tool'):
                version = line.split()[-1]
                if not version in supported_versions:
                    warnings.warn(
                          "PAR/REC version '%s' is currently not "
                          "supported -- making an attempt to read "
                          "nevertheless. Please email the NiBabel "
                          "mailing list, if you are interested in "
                          "adding support for this version."
                          % version)
            else:
                # just a comment
                continue
        elif line.startswith('.'):
            # read 'general information' and store in a dict
            first_colon = line[1:].find(':') + 1
            key = line[1:first_colon].strip()
            value = line[first_colon + 1:].strip()
            # get props for this hdr field
            props = _hdr_key_dict[key]
            # turn values into meaningful dtype
            if len(props) == 2:
                # only dtype spec and no shape
                value = props[1](value)
            elif len(props) == 3:
                # array with dtype and shape
                value = np.fromstring(value, props[1], sep=' ')
                value.shape = props[2]
            general_info[props[0]] = value
        elif line:
            # anything else is an image definition: store for later
            # processing
            image_info.append(line)

    # postproc image def props
    # create an array for all image defs
    image_defs = np.zeros(len(image_info), dtype=image_def_dtype)

    # for every image definition
    for i, line in enumerate(image_info):
        items = line.split()
        item_counter = 0
        # for all image properties we know about
        for props in image_def_dtd:
            if np.issubdtype(image_defs[props[0]].dtype, binary_type):
                # simple string
                image_defs[props[0]][i] = asbytes(items[item_counter])
                item_counter += 1
            elif len(props) == 2:
                # prop with numerical dtype
                if props[1] == 'S30':
                    1/0
                image_defs[props[0]][i] = props[1](items[item_counter])
                item_counter += 1
            elif len(props) == 3:
                # array prop with dtype
                nelements = np.prod(props[2])
                # get as many elements as necessary
                itms = items[item_counter:item_counter + nelements]
                # convert to array with dtype
                value = np.fromstring(" ".join(itms), props[1], sep=' ')
                # store
                image_defs[props[0]][i] = value
                item_counter += nelements

    return general_info, image_defs


class PARRECHeader(Header):
    """PAR/REC header"""
    def __init__(self, info, image_defs, default_scaling='dv'):
        """
        Parameters
        ----------
        info : dict
          "General information" from the PAR file (as returned by
          `parse_PAR_header()`).
        image_defs : array
          Structured array with image definitions from the PAR file (as
          returned by `parse_PAR_header()`).
        default_scaling : {'dv', 'fp'}
          Default scaling method to use for :meth:`get_slope_inter`` - see
          :meth:`get_data_scaling` for detail
        """
        self.general_info = info
        self.image_defs = image_defs
        self._slice_orientation = None
        self.default_scaling = default_scaling
        # charge with basic properties to be able to use base class
        # functionality
        # dtype
        dtype = np.typeDict[
            'int' + str(self._get_unique_image_prop('image pixel size')[0])]
        Header.__init__(self,
                        data_dtype=dtype,
                        shape=self.get_data_shape_in_file(),
                        zooms=self._get_zooms())

    @classmethod
    def from_header(klass, header=None):
        if header is None:
            raise PARRECError('Cannot create PARRECHeader from air.')
        if type(header) == klass:
            return header.copy()
        raise PARRECError('Cannot create PARREC header from '
                          'non-PARREC header.')

    @classmethod
    def from_fileobj(klass, fileobj):
        info, image_defs = parse_PAR_header(fileobj)
        return klass(info, image_defs)

    def copy(self):
        return PARRECHeader(deepcopy(self.general_info),
                            self.image_defs.copy())

    def _get_unique_image_prop(self, name):
        """Scan image definitions and return unique value of a property.

        If the requested property is an array this method does _not_ behave
        like `np.unique`. It will return the unique combination of all array
        elements for any image definition, and _not_ the unique element values.

        Parameters
        ----------
        name : str
            Name of the property

        Returns
        -------
        unique_value : array

        Raises
        ------
        If there is more than a single unique value a `PARRECError` is raised.
        """
        prop = self.image_defs[name]
        if len(prop.shape) > 1:
            uprops = [np.unique(prop[i]) for i in range(len(prop.shape))]
        else:
            uprops = [np.unique(prop)]
        if not np.prod([len(uprop) for uprop in uprops]) == 1:
            raise PARRECError('Varying %s in image sequence (%s). This is not '
                              'suppported.' % (name, uprops))
        else:
            return np.array([uprop[0] for uprop in uprops])

    def get_voxel_size(self):
        """Returns the spatial extent of a voxel.

        Does not include the slice gap in the slice extent.

        Returns
        -------
        vox_size: shape (3,) ndarray
        """
        # slice orientation for the whole image series
        slice_thickness = self._get_unique_image_prop('slice thickness')[0]
        voxsize_inplane = self._get_unique_image_prop('pixel spacing')
        voxsize = np.array((voxsize_inplane[0],
                            voxsize_inplane[1],
                            slice_thickness))
        return voxsize

    def get_data_offset(self):
        """ PAR header always has 0 data offset (into REC file) """
        return 0

    def set_data_offset(self, offset):
        """ PAR header always has 0 data offset (into REC file) """
        if offset != 0:
            raise PARRECError("PAR header assumes offset 0")

    def get_ndim(self):
        """Return the number of dimensions of the image data."""
        if self.general_info['max_dynamics'] > 1 \
           or self.general_info['max_gradient_orient'] > 1 \
           or self.general_info['max_echoes'] > 1:
            return 4
        else:
            return 3

    def _get_zooms(self):
        """Compute image zooms from header data.

        Spatial axis are first three.
        """
        # slice orientation for the whole image series
        slice_gap = self._get_unique_image_prop('slice gap')[0]
        # scaling per image axis
        zooms = np.ones(self.get_ndim())
        # spatial axes correspond to voxelsize + inter slice gap
        # voxel size (inplaneX, inplaneY, slices)
        zooms[:3] = self.get_voxel_size()
        zooms[2] += slice_gap
        # time axis?
        if len(zooms) > 3 and self.general_info['max_dynamics'] > 1:
            # DTI also has 4D
            # Convert time from milliseconds to seconds
            zooms[3] = self.general_info['repetition_time'] / 1000.
        # we leave it at the default (1) for 4D echo data
        return zooms

    def get_affine(self, origin='scanner'):
        """Compute affine transformation into scanner space.

        The method only considers global rotation and offset settings in the
        header and ignores potentially deviating information in the image
        definitions.

        Parameters
        ----------
        origin : {'scanner', 'fov'}
            Transformation origin. By default the transformation is computed
            relative to the scanner's iso center. If 'fov' is requested the
            transformation origin will be the center of the field of view
            instead.

        Returns
        -------
        aff : (4, 4) array
            4x4 array, with output axis order corresponding to RAS or (x,y,z)
            or (lr, pa, fh).

        Notes
        -----
        Transformations appear to be specified in (ap, fh, rl) axes.  The
        orientation of data is recorded in the "slice orientation" field of the
        PAR header "General Information".

        We need to:

        * translate to coordinates in terms of the center of the FOV
        * apply voxel size scaling
        * reorder / flip the data to Philips' PSL axes
        * apply the rotations
        * apply any isocenter scaling offset if `origin` == "scanner"
        * reorder and flip to RAS axes
        """
        # shape, zooms in original data ordering (ijk ordering)
        ijk_shape = np.array(self.get_data_shape()[:3])
        to_center = from_matvec(np.eye(3), -(ijk_shape - 1) / 2.)
        zoomer = np.diag(list(self.get_zooms()[:3]) + [1])
        slice_orientation = self.get_slice_orientation()
        permute_to_psl = ACQ_TO_PSL.get(slice_orientation)
        if permute_to_psl is None:
            raise PARRECError(
                "Unknown slice orientation ({0}).".format(slice_orientation))
        # hdr has deg, we need radians
        # Order is [ap, fh, rl]
        ang_rad = self.general_info['angulation'] * np.pi / 180.0
        # euler2mat accepts z, y, x angles and does rotation around z, y, x
        # axes in that order. It's possible that PAR assumes rotation in a
        # different order, we still need some relevant data to test this
        rot = from_matvec(euler2mat(*ang_rad[::-1]), [0, 0, 0])
        # compose the PSL affine
        psl_aff = dot_reduce(rot, permute_to_psl, zoomer, to_center)
        if origin == 'scanner':
            # offset to scanner's isocenter (in ap, fh, rl)
            iso_offset = self.general_info['off_center']
            psl_aff[:3, 3] += iso_offset
        # Currently in PSL; apply PSL -> RAS
        return np.dot(PSL_TO_RAS, psl_aff)

    def get_data_shape_in_file(self):
        """Return the shape of the binary blob in the REC file.

        Returns
        -------
        n_inplaneX : int
            number of voxels in X direction
        n_inplaneY : int
            number of voxels in Y direction
        n_slices : int
            number of slices
        n_vols : int
            number of dynamic scans, number of directions in diffusion, or
            number of echos
        """
        # e.g. number of volumes
        ndynamics = len(np.unique(self.image_defs['dynamic scan number']))
        # DTI volumes (b-values-1 x directions)
        # there is some awkward exception to this rule for b-values > 2
        # XXX need to get test image...
        ndtivolumes = ((self.general_info['max_diffusion_values'] - 1)
                       * self.general_info['max_gradient_orient'])
        nslices = len(np.unique(self.image_defs['slice number']))
        if not nslices == self.general_info['max_slices']:
            raise PARRECError("Header inconsistency: Found %i slices, "
                              "but header claims to have %i."
                              % (nslices, self.general_info['max_slices']))
        nechos = len(np.unique(self.image_defs['echo number']))

        # there should not be more than one: multiple dynamics, DTI, echos
        lens = [ndynamics, ndtivolumes, nechos]
        if sum(x > 1 for x in lens) > 1:
            raise RuntimeError('Cannot have multiple dynamics, dtivolumes, '
                               'or echos in the same file, found %s of each, '
                               'respectively' % lens)

        inplane_shape = tuple(self._get_unique_image_prop('recon resolution'))
        shape = inplane_shape + (nslices,)
        if ndynamics > 1:
            shape = shape + (ndynamics,)
        elif ndtivolumes > 1:
            shape = shape + (ndtivolumes,)
        elif nechos > 1:
            shape = shape + (nechos,)
        return shape

    def get_data_scaling(self, method="dv"):
        """Returns scaling slope and intercept.

        Parameters
        ----------
        method : {'fp', 'dv'}
          Scaling settings to be reported -- see notes below.

        Returns
        -------
        slope : float
            scaling slope
        intercept : float
            scaling intercept

        Notes
        -----
        The PAR header contains two different scaling settings: 'dv' (value on
        console) and 'fp' (floating point value). Here is how they are defined:

        PV: value in REC
        RS: rescale slope
        RI: rescale intercept
        SS: scale slope

        DV = PV * RS + RI
        FP = DV / (RS * SS)
        """
        # XXX: FP tends to become HUGE, DV seems to be more reasonable ->
        #      figure out which one means what

        # although the is a per-image scaling in the header, it looks like
        # there is just one unique factor and intercept per whole image series
        # XXX This is not always true, should throw exception if not
        scale_slope = self._get_unique_image_prop('scale slope')
        rescale_slope = self._get_unique_image_prop('rescale slope')
        rescale_intercept = self._get_unique_image_prop('rescale intercept')

        if method == 'dv':
            slope = rescale_slope
            intercept = rescale_intercept
        elif method == 'fp':
            # actual slopes per definition above
            slope = 1.0 / scale_slope
            # actual intercept per definition above
            intercept = rescale_intercept / (rescale_slope * scale_slope)
        else:
            raise ValueError("Unknown scling method '%s'." % method)
        return (slope, intercept)

    def get_slope_inter(self):
        """ Utility method to get default slope, intercept scaling
        """
        return tuple(
            np.asscalar(v)
            for v in self.get_data_scaling(method=self.default_scaling))

    def get_slice_orientation(self):
        """Returns the slice orientation label.

        Returns
        -------
        orientation : {'transverse', 'sagittal', 'coronal'}
        """
        if self._slice_orientation is None:
            self._slice_orientation = \
                slice_orientation_codes.label[
                    self._get_unique_image_prop('slice orientation')[0]]
        return self._slice_orientation

    def raw_data_from_fileobj(self, fileobj):
        ''' Read unscaled data array from `fileobj`

        Array axes correspond to x,y,z,t. For other orderings, you
        must reorder after the fact.

        Parameters
        ----------
        fileobj : file-like
            Must be open, and implement ``read`` and ``seek`` methods

        Returns
        -------
        arr : ndarray
           unscaled data array
        '''
        dtype = self.get_data_dtype()
        shape = self.get_data_shape()
        offset = self.get_data_offset()
        return array_from_file(shape, dtype, fileobj, offset)

    def data_from_fileobj(self, fileobj):
        ''' Read scaled data array from `fileobj`

        Use this routine to get the scaled image data from an image file
        `fileobj`, given a header `self`.  "Scaled" means, with any header
        scaling factors applied to the raw data in the file.  Use
        `raw_data_from_fileobj` to get the raw data.

        Parameters
        ----------
        fileobj : file-like
           Must be open, and implement ``read`` and ``seek`` methods

        Returns
        -------
        arr : ndarray
           scaled data array
        '''
        # read unscaled data
        data = self.raw_data_from_fileobj(fileobj)
        # get scalings from header.  Value of None means not present in header
        slope, inter = self.get_slope_inter()
        slope = 1.0 if slope is None else slope
        inter = 0.0 if inter is None else inter
        # Upcast as necessary for big slopes, intercepts
        return apply_read_scaling(data, slope, inter)


class PARRECImage(SpatialImage):
    """PAR/REC image"""
    header_class = PARRECHeader
    files_types = (('image', '.rec'), ('header', '.par'))

    ImageArrayProxy = ArrayProxy

    @classmethod
    def from_file_map(klass, file_map):
        with file_map['header'].get_prepare_fileobj('rt') as hdr_fobj:
            hdr = klass.header_class.from_fileobj(hdr_fobj)
        rec_fobj = file_map['image'].get_prepare_fileobj()
        data = klass.ImageArrayProxy(rec_fobj, hdr)
        return klass(data,
                     hdr.get_affine(),
                     header=hdr,
                     extra=None,
                     file_map=file_map)


load = PARRECImage.load
