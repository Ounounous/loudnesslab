"""Sonoridad de Zwicker, ISO 532-1:2017 método estacionario, desde niveles de tercio de octava.

Las tres funciones privadas (_main_loudness, _calc_slopes, _get_rns_index) están copiadas sin cambios
de lógica de MoSQITo 1.2.1 (https://github.com/Eomys/MoSQITo, sq_metrics/loudness/loudness_zwst),
Copyright (c) MoSQITo Developers, bajo licencia Apache 2.0 (ver LICENCIA_MOSQITO.txt). Cambios: solo
los imports (los tres archivos en uno). MoSQITo las valida contra los casos de prueba de la norma.

  sonoridad(niveles)   N (sone) de cada fila: niveles en dB SPL de las 28 bandas de 25 Hz a 12,5 kHz.
  sone_a_phon(n)       nivel de sonoridad LN (phon).

Se calcula por lotes: cada llamada tiene un costo fijo de ~50 ms, y 2000 filas tardan ~0,2 s.
"""

import numpy as np
from numpy import (append, arange, array, copy, divide, floor, int32, log10, logical_and, logical_not,  # noqa: F401
                   logical_xor, max, maximum, minimum, multiply, newaxis, ones, power, roll, round, squeeze, tile,
                   zeros)

N_BANDAS_ZW = 28          # 25 Hz … 12,5 kHz (la banda de 16 kHz no entra en ISO 532-1)
LOTE = 3000               # filas por llamada (la memoria crece con el lote: ~6 matrices de 240 × LOTE)
CAMPOS = {"libre": "free", "difuso": "diffuse"}


def sonoridad(niveles, campo="libre"):
    """N (sone) por fila. `niveles`: (n, 28) o (28,), dB SPL por banda. Lo que queda bajo el umbral
    no aporta (se recorta a −20 dB para evitar −inf)."""
    x = np.atleast_2d(np.asarray(niveles, dtype=float))[:, :N_BANDAS_ZW]
    x = np.maximum(x, -20.0)
    out = np.empty(len(x))
    for i in range(0, len(x), LOTE):
        trozo = x[i:i + LOTE].T.copy()
        if trozo.shape[1] == 1:       # (las funciones de MoSQITo tratan aparte una sola fila)
            trozo = np.repeat(trozo, 2, axis=1)
        n, _ = _calc_slopes(_main_loudness(trozo, CAMPOS.get(campo, campo)))
        out[i:i + LOTE] = np.atleast_1d(n)[:min(LOTE, len(x) - i)]
    return out


def sone_a_phon(n):
    """ISO 532-1: LN = 40 + 33,22·log10(N) para N ≥ 1 sone; 40·(N + 0,0005)^0,35 bajo 1 sone."""
    n = np.asarray(n, dtype=float)
    return np.where(n >= 1, 40 + 33.22 * np.log10(np.maximum(n, 1e-9)), 40 * (np.maximum(n, 0) + 0.0005) ** 0.35)


# ------------------------------------------------------------------------------ MoSQITo 1.2.1

def _main_loudness(spec_third, field_type):
    """Calculate core loudness

    The code is based on BASIC program published in "Program for
    calculating loudness according to DIN 45631 (ISO 532-1:2017)", E.Zwicker
    and H.Fastl, J.A.S.J (E) 12, 1 (1991).
    It also corresponds to the following functions of the C program
    published with ISO 532-1:2017:
    - corr_third_octave_intensities
    - f_calc_lcbs
    - f_calc_core_loudness
    - f_corr_loudness

    Parameters
    ----------
    spec_third : numpy.ndarray
        A third octave band spectrum with size (nfreq, nseg) or (nfreq,) [dB ref. 2e-5 Pa]
    field_type : str
        Type of soundfield correspondin to spec_third ("free" or
        "diffuse")

    Outputs
    -------
    nm :  numpy.ndarray
        Core loudness
    """
    # Ref. ISO 532-1:2017 paragraph A.3
    # The table A.3 giving the weights of the 1/3 band levels for
    # center freq. below 300 Hz is only specified for levels up to 120 dB
    # If one of the first 11 bands (from 25 to 250 Hz) exceed 120 dB the
    # Zwicker method cannot be applied.
    if max(spec_third[0:11]) > 120.0:
        raise ValueError(
            "1/3 octave band value exceed 120 dB, for which "
            + "the Zwicker method is no longer valid."
        )
    #
    # Date tables definition (variable names and description according to
    # Zwicker:1991)
    # Ranges of 1/3 octave band levels for correction at low frequencies
    # according to equal loudness contours
    rap = array([45, 55, 65, 71, 80, 90, 100, 120])
    # Reduction of 1/3 octave band levels at low frequencies according to
    # equal loudness contours within the eight ranges defined by RAP
    dll = array(
        [
            (-32, -24, -16, -10, -5, 0, -7, -3, 0, -2, 0),
            (-29, -22, -15, -10, -4, 0, -7, -2, 0, -2, 0),
            (-27, -19, -14, -9, -4, 0, -6, -2, 0, -2, 0),
            (-25, -17, -12, -9, -3, 0, -5, -2, 0, -2, 0),
            (-23, -16, -11, -7, -3, 0, -4, -1, 0, -1, 0),
            (-20, -14, -10, -6, -3, 0, -4, -1, 0, -1, 0),
            (-18, -12, -9, -6, -2, 0, -3, -1, 0, -1, 0),
            (-15, -10, -8, -4, -2, 0, -3, -1, 0, -1, 0),
        ]
    )
    # Critical band level at absolute threshold without taking into
    # account the transmission characteristics of the ear
    ltq = array([30, 18, 12, 8, 7, 6, 5, 4, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3])
    # Correction of levels according to the transmission characteristics
    # of the ear
    a0 = array(
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -0.5, -1.6, -3.2, -5.4, -5.6, -4, -1.5, 2, 5, 12]
    )
    # Level difference between free and diffuse sound fields
    ddf = array(
        [
            0,
            0,
            0.5,
            0.9,
            1.2,
            1.6,
            2.3,
            2.8,
            3,
            2,
            0,
            -1.4,
            -2,
            -1.9,
            -1,
            0.5,
            3,
            4,
            4.3,
            4,
        ]
    )
    # Adaptation of 1/3 oct. band levels to the corresponding critical
    # band level
    dcb = array(
        [
            -0.25,
            -0.6,
            -0.8,
            -0.8,
            -0.5,
            0,
            0.5,
            1.1,
            1.5,
            1.7,
            1.8,
            1.8,
            1.7,
            1.6,
            1.4,
            1.2,
            0.8,
            0.5,
            0,
            -0.5,
        ]
    )
    #
    # Correction of 1/3 oct. band levels according to equal loudness
    # contours 'xp' and calculation of the intensities for 1/3 oct.
    # bands up to 315 Hz

    # 1-dimensional array to 2-dimensional array with size (nfreq, 1)
    if spec_third.ndim == 1:
        spec_third = spec_third[:, newaxis]

    spec_third_aux = spec_third[: dll.shape[1], :]
    # spec_third_aux[:, -1] = 0

    # Convert rap, dll in 3 dimensional array
    # 1. generate the array shape
    base_mat = ones((dll.shape[0], dll.shape[1], spec_third_aux.shape[1]))
    # 2. start saving data in rap and DLL array
    rap_mat = array(
        [base_mat[:, i, :].T * rap for i in arange(dll.shape[1])]
    ).transpose(2, 0, 1)
    dll_mat = array(
        [multiply(base_mat[:, :, i], dll) for i in arange(spec_third.shape[1])]
    ).transpose(1, 2, 0)
    spec_third_aux_mat = array(
        [
            multiply(base_mat[i, :, :], spec_third_aux)
            for i in arange(dll.shape[0])
        ]
    )
    # create the the array rap-dll
    rap_dll_mat = rap_mat - dll_mat

    # This part substitutes the while loop.
    # create the mask to operate
    logic_mat = spec_third_aux_mat > rap_dll_mat
    dll_result = dll_mat[0, :, :]
    dll_result[logic_mat[0, :, :]] = 0
    for i in arange(1, dll_mat.shape[0] - 1):
        mask = logical_xor(logic_mat[i - 1, :, :], logic_mat[i, :, :])
        dll_result[mask] = dll_mat[i, mask]

    xp = dll_result + spec_third_aux
    ti = power(10, (xp / 10))

    # Determination of levels LCB(1), LCB(2) and LCB(3) within the
    # first three critical bands

    gi = zeros([3, dll_result.shape[1]])
    gi[0, :] = ti[0:6, :].sum(axis=0)
    gi[1, :] = ti[6:9, :].sum(axis=0)
    gi[2, :] = ti[9:11, :].sum(axis=0)

    logic_gi = gi > 0
    lcb = zeros([3, dll_result.shape[1]])
    lcb[logic_gi] = 10 * log10(gi[logic_gi])

    # Calculation of main loudness
    s = 0.25
    nm = zeros([20, spec_third_aux.shape[1]])
    le = copy(spec_third[8:, :])
    # le = le.reshape((20))
    le[0:3, :] = lcb
    a0_mat = ones(a0.shape[0] * spec_third.shape[1]).reshape(
        a0.shape[0], spec_third.shape[1]
    )
    a0_mat = (a0_mat.T * a0).T
    le = le - a0_mat

    if field_type == "diffuse":
        ddf_mat = ones(ddf.shape[0] * spec_third.shape[1]).reshape(
            ddf.shape[0], spec_third.shape[1]
        )
        ddf_mat = (ddf_mat.T * ddf).T
        le += ddf_mat

    ltq_mat = ones(ltq.shape[0] * spec_third.shape[1]).reshape(
        ltq.shape[0], spec_third.shape[1]
    )
    ltq_mat = (ltq_mat.T * ltq).T
    i = le > ltq_mat
    dcb_mat = ones(dcb.shape[0] * spec_third.shape[1]).reshape(
        dcb.shape[0], spec_third.shape[1]
    )
    dcb_mat = (dcb_mat.T * dcb).T
    le[i] -= dcb_mat[i]

    mp1 = 0.0635 * power(10, 0.025 * ltq_mat)
    mp1[i == False] = 0
    mp2 = power(1 - s + s * power(10, 0.1 * (le - ltq_mat)), 0.25) - 1
    mp2[i == False] = 0

    nm = multiply(mp1, mp2)

    nm[i == False] = 0
    nm[nm < 0] = 0
    current_shape = nm.shape
    nm = append(nm, zeros(current_shape[1])).reshape(
        current_shape[0] + 1, current_shape[1]
    )
    #
    # Correction of specific loudness in the lowest critical band
    # taking into account the dependance of absolute threshold
    # within this critical band
    korry = 0.4 + 0.32 * nm[0] ** 0.2
    nm[0, korry <= 1] *= korry[korry <= 1]
    # nm[:, -1] = 0
    # if spec_third.ndim == 1 or spec_third.shape[1] == 1:
    #     # This is only for test_loudness_zwicker_3oct because only one array of one col is given and this routine needs 2 or more
    #     # This line is only also for testing test_loudness_zwicker_wav(), only in case one col in spec third is given.
    #     nm = nm[:, 1]

    return squeeze(nm)


def _get_rns_index(array_nm, vector_rns, equal_too=False):
    """Function that returns the index in the array vector_rns for each value of srray _nm

    Parameters
    ----------
    array_nm : numpy.ndarray, values of the matrix toget the indexes

    vector_rns:reference vector to get indexes
    equal_too : boolean

    Outputs
    -------
    indexes :  numpy.ndarray
        Array of indexes
    """

    if len(array_nm.shape) == 1:
        (wide,) = array_nm.shape
        (deep,) = vector_rns.shape
        array_aux = round(tile(array_nm, [deep, 1]), 8)
        rns_array = round(ones([wide, deep]) * vector_rns, 8).T
    else:
        wide, length = array_nm.shape
        (deep,) = vector_rns.shape
        array_aux = round(tile(array_nm, [deep, 1, 1]), 8)
        rns_array = round((ones([wide, length, deep]) * vector_rns), 8).transpose(
            2, 0, 1
        )
        # indexes = (array_aux < rns_array).sum(axis=0)
        # indexes[indexes==18] = 17
    if equal_too:
        indexes = (array_aux <= rns_array).sum(axis=0)
    else:
        indexes = (array_aux < rns_array).sum(axis=0)
    indexes[indexes == 18] = 17

    return indexes


def _calc_slopes(nm):
    """"""
    # Upper limits of approximated critical bands in terms of critical
    # band rate
    zup = array(
        [
            0.9,
            1.8,
            2.8,
            3.5,
            4.4,
            5.4,
            6.6,
            7.9,
            9.2,
            10.6,
            12.3,
            13.8,
            15.2,
            16.7,
            18.1,
            19.3,
            20.6,
            21.8,
            22.7,
            23.6,
            24,
        ]
    )
    # Range of specific loudness for the determination of the steepness
    # of the upper slopes in the specific loudness - critical band rate
    # pattern
    rns = array(
        [
            21.5,
            18,
            15.1,
            11.5,
            9,
            6.1,
            4.4,
            3.1,
            2.13,
            1.36,
            0.82,
            0.42,
            0.30,
            0.22,
            0.15,
            0.10,
            0.035,
            0,
        ]
    )
    # Steepness of the upper slopes in the specific loudness = Critical
    # band rate pattern for the ranges 'rns' as a function of the number
    # of the critical band
    usl = array(
        [
            (13, 8.2, 6.3, 5.5, 5.5, 5.5, 5.5, 5.5),
            (9, 7.5, 6, 5.1, 4.5, 4.5, 4.5, 4.5),
            (7.8, 6.7, 5.6, 4.9, 4.4, 3.9, 3.9, 3.9),
            (6.2, 5.4, 4.6, 4.0, 3.5, 3.2, 3.2, 3.2),
            (4.5, 3.8, 3.6, 3.2, 2.9, 2.7, 2.7, 2.7),
            (3.7, 3.0, 2.8, 2.35, 2.2, 2.2, 2.2, 2.2),
            (2.9, 2.3, 2.1, 1.9, 1.8, 1.7, 1.7, 1.7),
            (2.4, 1.7, 1.5, 1.35, 1.3, 1.3, 1.3, 1.3),
            (1.95, 1.45, 1.3, 1.15, 1.1, 1.1, 1.1, 1.1),
            (1.5, 1.2, 0.94, 0.86, 0.82, 0.82, 0.82, 0.82),
            (0.72, 0.67, 0.64, 0.63, 0.62, 0.62, 0.62, 0.62),
            (0.59, 0.53, 0.51, 0.50, 0.42, 0.42, 0.42, 0.42),
            (0.40, 0.33, 0.26, 0.24, 0.24, 0.22, 0.22, 0.22),
            (0.27, 0.21, 0.20, 0.18, 0.17, 0.17, 0.17, 0.17),
            (0.16, 0.15, 0.14, 0.12, 0.11, 0.11, 0.11, 0.11),
            (0.12, 0.11, 0.10, 0.08, 0.08, 0.08, 0.08, 0.08),
            (0.09, 0.08, 0.07, 0.06, 0.06, 0.06, 0.06, 0.05),
            (0.06, 0.05, 0.03, 0.02, 0.02, 0.02, 0.02, 0.02),
        ]
    )

    # From Ernesto Avedillo 13/feb/2022
    # Considering the original routine if ig > 7:ig = 7 until ig = 21 so I can append the last column until usl.shape = (18,21)
    usl_reshaped = append(usl, tile(usl[:, 7], 13).reshape(13, 18).T, axis=1)

    # For test, in case nm has only 1 col.
    len_1_nm = False
    if len(nm.shape) == 1:
        len_1_nm = True
        nm = append(nm, nm).reshape(2, 21).T

    # Start values
    data_length = nm.shape[1]
    nm_wide = nm.shape[0]
    spec_length = 240
    dec_compare = 8
    # Working array variables
    n2_array_specific = ones((spec_length, data_length))
    z2_array_specific = ones((spec_length, data_length))
    usl_array_specific = ones((spec_length, data_length))
    dz_array_specific = ones((spec_length, data_length))
    rns_values_specific = ones((spec_length, data_length))
    # Create a zup vector called zup_ea to define the position in the N_spezific array.
    zup_ea = (copy(zup) * 10).astype(int32)
    zup_ea = append(zup_ea, 0)

    # Create a complete array of ZUP vectors with shape nm elements in a row
    zup_array_ea = (ones((nm_wide, data_length)).T * zup).T
    # crearte an array of z values 0.1 increase

    # Prepare the array N_specific for output
    # For all cases where nm[:,col-1] < nm[:,col]
    N_specific = zeros((spec_length, data_length))
    # I save the first values for first raws defined in zup_ea
    # N_specific[:zup_ea[0],:] = np.multiply(N_specific[:zup_ea[0],:] , nm[1])
    # I complete the rest of the matrix extending the array 21  to 240
    for i in arange(nm_wide - 1):
        N_specific[zup_ea[i - 1] : zup_ea[i], :] = nm[i]

    N = zeros(data_length)

    # nm_aux = np.copy(nm)
    # nm_aux [0] = nm[1]

    # obtain the rns indexes for each value of the nm matrix.
    # rns_ind = _get_rns_index (nm, rns ,equal_too = True )
    rns_ind = _get_rns_index(nm, rns)
    # save in an array the values corresponding to this index
    rns_values = rns[rns_ind]

    # search usl_array for each nm cell
    usl_array_ind = array(
        [rns_ind.T, ones((zup.shape[0], data_length)).T * arange(21)], dtype=int
    ).transpose(1, 2, 0)
    usl_array = usl_reshaped[usl_array_ind[:, :, 0], usl_array_ind[:, :, 1]].T
    # create a 240 x nm shape[1] array to save the dz values equal to z2-z1
    dz = zeros((nm_wide, data_length))
    dz = zup_array_ea - roll(zup_array_ea, 1, axis=0)

    dz[0, :] = zup[1]
    dz[1, :] = zup[1]

    for i in arange(nm_wide):
        n2_array_specific[zup_ea[i - 1] : zup_ea[i], :] = nm[i]
        dz_array_specific[zup_ea[i - 1] : zup_ea[i], :] = dz[i]
        z2_array_specific[zup_ea[i - 1] : zup_ea[i], :] = zup_array_ea[i]
        usl_array_specific[zup_ea[i - 1] : zup_ea[i], :] = usl_array[i]
        rns_values_specific[zup_ea[i - 1] : zup_ea[i], :] = rns_values[i]

    j = 1
    n1_aux = zeros(data_length)
    z1_aux = zeros(data_length)

    for i in arange(nm_wide):
        # for all cases where nm[i-1]>nm[i]
        j = zup_ea[i - 1]
        indexes = _get_rns_index(n2_array_specific[j - 1], rns)
        rns_values_specific[j] = rns[indexes]
        usl_array_specific[j] = usl_reshaped[indexes, i - 1]

        mask_n1_bigger_nm = round(n2_array_specific[j - 1], dec_compare) > round(
            nm[i], dec_compare
        )
        # For all n1 <= nm[i] calculate (N = N + n2 * (z2 - z1))
        N[logical_not(mask_n1_bigger_nm)] += (
            n2_array_specific[j] * (z2_array_specific[j] - z1_aux)
        )[logical_not(mask_n1_bigger_nm)]
        n1_aux[logical_not(mask_n1_bigger_nm)] = copy(n2_array_specific[j])[
            logical_not(mask_n1_bigger_nm)
        ]
        z1_aux[logical_not(mask_n1_bigger_nm)] = copy(z2_array_specific[j])[
            logical_not(mask_n1_bigger_nm)
        ]

        if mask_n1_bigger_nm.sum() > 0:
            # For all n1 > nm[i] calculate z2,dz n2.
            # This routine
            # n2 = rns[j]
            # if n2 < nm[i]:
            #     n2 = nm[i]
            # dz = (n1 - n2) / usl[j, ig]
            # z2 = z1 + dz
            # if z2 > zup[i]:
            #     z2 = zup[i]
            #     dz = z2 - z1
            #     n2 = n1 - dz * usl[j, ig]
            # Can be subtituted by
            # max_rns_nm = max(rns[j],nm[i])
            # z2_ea = min((n1-max_rns_nm)/usl[j,ig]+z1,zup[i])
            # dz_ea = z2_ea - z1
            # n2_ea = n1 - dz_ea * usl[j,ig]

            Max_rns_nm_Array = maximum(rns_values_specific[j - 1], nm[i])
            z2_array_specific[j, mask_n1_bigger_nm] = minimum(
                divide(n1_aux - Max_rns_nm_Array,
                          usl_array_specific[j]) + z1_aux,
                zup[i],
            )[mask_n1_bigger_nm]
            dz_array_specific[j, mask_n1_bigger_nm] = (z2_array_specific[j] - z1_aux)[
                mask_n1_bigger_nm
            ]
            n2_array_specific[j, mask_n1_bigger_nm] = (
                n1_aux -
                multiply(dz_array_specific[j], usl_array_specific[j])
            )[mask_n1_bigger_nm]

            # Calculate N for all n1 > nm[i]

            N[mask_n1_bigger_nm] += (
                dz_array_specific[j] * (n1_aux + n2_array_specific[j]) / 2
            )[mask_n1_bigger_nm]

            # get value of z
            z_array = ones(data_length) * zup[i - 1] + 0.1

            for j in arange(zup_ea[i - 1], zup_ea[i]):

                # Save values from previous calculation (only after second loop)
                if j != zup_ea[i - 1]:
                    z2_array_specific[j, mask_n1_bigger_nm] = z2_array_specific[
                        j - 1, mask_n1_bigger_nm
                    ]
                    n2_array_specific[j, mask_n1_bigger_nm] = n2_array_specific[
                        j - 1, mask_n1_bigger_nm
                    ]
                    dz_array_specific[j, mask_n1_bigger_nm] = dz_array_specific[
                        j - 1, mask_n1_bigger_nm
                    ]
                    usl_array_specific[j, mask_n1_bigger_nm] = usl_array_specific[
                        j - 1, mask_n1_bigger_nm
                    ]
                    rns_values_specific[j, mask_n1_bigger_nm] = rns_values_specific[
                        j - 1, mask_n1_bigger_nm
                    ]

                # Sometimes a second loop is necesary if z > z2
                mask_z_bigger_z2 = logical_and(
                    mask_n1_bigger_nm,
                    round(z2_array_specific[j], dec_compare)
                    <= round(z_array, dec_compare),
                )
                if mask_z_bigger_z2.sum() > 0:
                    indexes = _get_rns_index(
                        n2_array_specific[j,
                                          mask_z_bigger_z2], rns, equal_too=True
                    )
                    rns_values_specific[j, mask_z_bigger_z2] = rns[indexes]
                    usl_array_specific[j, mask_z_bigger_z2] = usl_reshaped[
                        indexes, i - 1
                    ]
                    # Save Copy de Z2 y N2
                    n1_aux[mask_z_bigger_z2] = n2_array_specific[j,
                                                                 mask_z_bigger_z2]
                    z1_aux[mask_z_bigger_z2] = z2_array_specific[j,
                                                                 mask_z_bigger_z2]

                    # Vuelvo al inicio del loop y reviso de nuevo que n2 sea menor que nm
                    # Vuelvo a calcular mask_n1_bigger_nm
                    # Para los valores de n1_aux<=nm[i]
                    mask_z_bigger_z2_1 = logical_and(
                        mask_z_bigger_z2,
                        round(n1_aux, dec_compare) <= round(
                            nm[i], dec_compare),
                    )
                    n2_array_specific[j, mask_z_bigger_z2_1] = nm[i,
                                                                  mask_z_bigger_z2_1]
                    z2_array_specific[j, mask_z_bigger_z2_1] = zup[i]
                    dz_array_specific[j, mask_z_bigger_z2_1] = (
                        z2_array_specific[j] - z1_aux
                    )[mask_z_bigger_z2_1]
                    # Recalculate N
                    N[mask_z_bigger_z2_1] += (
                        n2_array_specific[j] * (z2_array_specific[j] - z1_aux)
                    )[mask_z_bigger_z2_1]

                    # For Values n1_aux>nm[i] after second loop
                    mask_z_bigger_z2_2 = logical_and(
                        mask_z_bigger_z2,
                        round(n1_aux, dec_compare) > round(
                            nm[i], dec_compare),
                    )
                    Max_rns_nm_Array = maximum(
                        rns_values_specific[j], nm[i])
                    z2_array_specific[j, mask_z_bigger_z2_2] = minimum(
                        divide(n1_aux - Max_rns_nm_Array,
                                  usl_array_specific[j])
                        + z1_aux,
                        zup[i],
                    )[mask_z_bigger_z2_2]
                    dz_array_specific[j, mask_z_bigger_z2_2] = (
                        z2_array_specific[j] - z1_aux
                    )[mask_z_bigger_z2_2]
                    n2_array_specific[j, mask_z_bigger_z2_2] = (
                        n1_aux
                        - multiply(dz_array_specific[j], usl_array_specific[j])
                    )[mask_z_bigger_z2_2]
                    # Recalculate N
                    N[mask_z_bigger_z2_2] += (
                        dz_array_specific[j] *
                        (n1_aux + n2_array_specific[j]) / 2
                    )[mask_z_bigger_z2_2]
                    N_specific[j, mask_z_bigger_z2_2] = (
                        n1_aux - multiply((z_array - z1_aux),
                                             usl_array_specific[j])
                    )[mask_z_bigger_z2_2]

                    # For the rest of the values  Where z < z2 calculate N_Specific
                    mask_z_rest = logical_xor(
                        mask_z_bigger_z2, mask_n1_bigger_nm)
                    N_specific[j, mask_z_rest] = (
                        n1_aux - multiply((z_array - z1_aux),
                                             usl_array_specific[j])
                    )[mask_z_rest]
                    z_array += 0.1

                    # Generate a new mask for n1 > nm[i]
                    mask_n1_bigger_nm = logical_xor(
                        mask_n1_bigger_nm, mask_z_bigger_z2_1
                    )

                else:
                    N_specific[j, mask_n1_bigger_nm] = (
                        n1_aux - multiply((z_array - z1_aux),
                                             usl_array_specific[j])
                    )[mask_n1_bigger_nm]
                    z_array += 0.1

                z1_aux = copy(z2_array_specific[j])
                n1_aux = copy(n2_array_specific[j])

                if mask_n1_bigger_nm.sum() == 0:
                    break
            z1_aux = copy(z2_array_specific[zup_ea[i] - 1])
            n1_aux = copy(n2_array_specific[zup_ea[i] - 1])

            indexes = _get_rns_index(
                n2_array_specific[j, mask_z_bigger_z2], rns, equal_too=True
            )
            rns_values_specific[j, mask_z_bigger_z2] = rns[indexes]
            usl_array_specific[j,
                               mask_z_bigger_z2] = usl_reshaped[indexes, i - 1]

    N[N < 0] = 0
    N[N <= 16] = floor(N[N <= 16] * 1000 + 0.5) / 1000
    N[N > 16] = floor(N[N > 16] * 100 + 0.5) / 100
    if len_1_nm:
        N = N[0]
        N_specific = N_specific[:, 0]

    return N, N_specific
