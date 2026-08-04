export type Point = { x: number; y: number };

export type SimilarityTransform = {
    a: number;
    b: number;
    tx: number;
    ty: number;
};

// Solves for a similarity transform (uniform scale + rotation + translation,
// no shear/reflection) mapping each `src[i]` to the corresponding `dst[i]`,
// via linear least squares. The transform is linear in its 4 unknowns:
//   x' = a*x - b*y + tx
//   y' = b*x + a*y + ty
// so N correspondences give a 2N-equation system solved via the normal
// equations of a 4x4 matrix — the standard closed form for this class of
// face-alignment problem (a similarity-only restriction of 2D Procrustes).
// Needs at least 2 points; exact for 2, least-squares for more (e.g. 5).
export const solveSimilarityTransform = (src: Point[], dst: Point[]): SimilarityTransform | null => {
    const n = Math.min(src.length, dst.length);
    if (n < 2) {
        return null;
    }

    let sxx = 0;
    let syy = 0;
    let sx = 0;
    let sy = 0;
    let sxu = 0;
    let syv = 0;
    let syu = 0;
    let sxv = 0;
    let su = 0;
    let sv = 0;

    for (let i = 0; i < n; i += 1) {
        const x = src[i].x;
        const y = src[i].y;
        const u = dst[i].x;
        const v = dst[i].y;
        sxx += x * x;
        syy += y * y;
        sx += x;
        sy += y;
        sxu += x * u;
        syv += y * v;
        syu += y * u;
        sxv += x * v;
        su += u;
        sv += v;
    }

    // Normal-equations matrix for unknowns [a, b, tx, ty]^T, derived from
    // stacking rows [x,-y,1,0]->u and [y,x,0,1]->v per point and forming A^T A.
    const m: number[][] = [
        [sxx + syy, 0, sx, sy],
        [0, sxx + syy, -sy, sx],
        [sx, -sy, n, 0],
        [sy, sx, 0, n],
    ];
    const rhs = [sxu + syv, sxv - syu, su, sv];

    const solution = solveLinearSystem(m, rhs);
    if (!solution || !solution.every(Number.isFinite)) {
        return null;
    }
    const [a, b, tx, ty] = solution;
    return { a, b, tx, ty };
};

// Applies a solved similarity transform to a point (mainly for testing / sanity checks).
export const applySimilarityTransform = (transform: SimilarityTransform, point: Point): Point => ({
    x: transform.a * point.x - transform.b * point.y + transform.tx,
    y: transform.b * point.x + transform.a * point.y + transform.ty,
});

// Small fixed-size (4x4) Gaussian elimination with partial pivoting. General
// enough to keep this readable without pulling in a linear-algebra library
// for a 4-unknown problem.
const solveLinearSystem = (matrix: number[][], rhs: number[]): number[] | null => {
    const size = rhs.length;
    const m = matrix.map((row, i) => [...row, rhs[i]]);

    for (let col = 0; col < size; col += 1) {
        let pivotRow = col;
        let pivotValue = Math.abs(m[col][col]);
        for (let row = col + 1; row < size; row += 1) {
            const value = Math.abs(m[row][col]);
            if (value > pivotValue) {
                pivotValue = value;
                pivotRow = row;
            }
        }
        if (pivotValue < 1e-9) {
            return null;
        }
        if (pivotRow !== col) {
            const temp = m[col];
            m[col] = m[pivotRow];
            m[pivotRow] = temp;
        }
        for (let row = col + 1; row < size; row += 1) {
            const factor = m[row][col] / m[col][col];
            for (let c = col; c <= size; c += 1) {
                m[row][c] -= factor * m[col][c];
            }
        }
    }

    const solution = new Array(size).fill(0);
    for (let row = size - 1; row >= 0; row -= 1) {
        let sum = m[row][size];
        for (let col = row + 1; col < size; col += 1) {
            sum -= m[row][col] * solution[col];
        }
        solution[row] = sum / m[row][row];
    }
    return solution;
};

// Standard 112x112 ArcFace/InsightFace 5-point reference template (widely
// reproduced across open-source ArcFace alignment implementations). Order:
// [rightEye, leftEye, nose, leftMouth, rightMouth] using subject-anatomical
// left/right (matches dlib/face-api's 68-point eye/mouth indexing: right eye
// = indices 36-41, left eye = 42-47).
// CAUTION: not yet visually validated against this app's actual pipeline —
// the prior (dead, never-exercised) 2-point alignment code used a different
// eye Y-ratio (0.38 of canvas height vs this template's ~0.46), so confirm by
// rendering a few real aligned crops before trusting this for a full re-embed.
export const ARC_FACE_5POINT_TEMPLATE: Point[] = [
    { x: 38.2946, y: 51.6963 }, // rightEye
    { x: 73.5318, y: 51.5014 }, // leftEye
    { x: 56.0252, y: 71.7366 }, // nose
    { x: 41.5493, y: 92.3655 }, // leftMouth
    { x: 70.7299, y: 92.2041 }, // rightMouth
];
