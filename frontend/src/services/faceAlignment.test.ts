import { describe, expect, it } from 'vitest';
import { applySimilarityTransform, solveSimilarityTransform, type Point } from './faceAlignment';

const applyKnownTransform = (points: Point[], scale: number, angleRad: number, tx: number, ty: number): Point[] => {
    const cos = Math.cos(angleRad);
    const sin = Math.sin(angleRad);
    return points.map(({ x, y }) => ({
        x: scale * (cos * x - sin * y) + tx,
        y: scale * (sin * x + cos * y) + ty,
    }));
};

describe('solveSimilarityTransform', () => {
    it('returns null with fewer than 2 points', () => {
        expect(solveSimilarityTransform([], [])).toBeNull();
        expect(solveSimilarityTransform([{ x: 0, y: 0 }], [{ x: 1, y: 1 }])).toBeNull();
    });

    it('recovers the identity transform when src equals dst', () => {
        const points: Point[] = [
            { x: 10, y: 20 },
            { x: 30, y: 5 },
            { x: 15, y: 40 },
        ];
        const transform = solveSimilarityTransform(points, points);
        expect(transform).not.toBeNull();
        expect(transform!.a).toBeCloseTo(1, 6);
        expect(transform!.b).toBeCloseTo(0, 6);
        expect(transform!.tx).toBeCloseTo(0, 6);
        expect(transform!.ty).toBeCloseTo(0, 6);
    });

    it('recovers a known rotation + scale + translation exactly for 2 points', () => {
        const src: Point[] = [
            { x: 0, y: 0 },
            { x: 10, y: 0 },
        ];
        const scale = 2.5;
        const angle = Math.PI / 6; // 30 degrees
        const tx = 50;
        const ty = -20;
        const dst = applyKnownTransform(src, scale, angle, tx, ty);

        const transform = solveSimilarityTransform(src, dst);
        expect(transform).not.toBeNull();

        // The recovered (a, b) encode scale*cos(theta) / scale*sin(theta).
        const recoveredScale = Math.hypot(transform!.a, transform!.b);
        const recoveredAngle = Math.atan2(transform!.b, transform!.a);
        expect(recoveredScale).toBeCloseTo(scale, 6);
        expect(recoveredAngle).toBeCloseTo(angle, 6);
        expect(transform!.tx).toBeCloseTo(tx, 6);
        expect(transform!.ty).toBeCloseTo(ty, 6);
    });

    it('least-squares fits 5 noisy correspondences close to the true transform', () => {
        const src: Point[] = [
            { x: 38.2946, y: 51.6963 },
            { x: 73.5318, y: 51.5014 },
            { x: 56.0252, y: 71.7366 },
            { x: 41.5493, y: 92.3655 },
            { x: 70.7299, y: 92.2041 },
        ];
        const scale = 1.4;
        const angle = -0.08;
        const tx = 12;
        const ty = 8;
        const exactDst = applyKnownTransform(src, scale, angle, tx, ty);
        // Perturb one point slightly to simulate imperfect landmark detection.
        const noisyDst = exactDst.map((p, i) => (i === 2 ? { x: p.x + 1.5, y: p.y - 1.2 } : p));

        const transform = solveSimilarityTransform(src, noisyDst);
        expect(transform).not.toBeNull();

        // Applying the fitted transform back to src should land close to the
        // (mostly exact, one noisy) targets — not pixel-perfect, but close.
        src.forEach((point, i) => {
            const projected = applySimilarityTransform(transform!, point);
            expect(Math.abs(projected.x - noisyDst[i].x)).toBeLessThan(2);
            expect(Math.abs(projected.y - noisyDst[i].y)).toBeLessThan(2);
        });
    });
});
