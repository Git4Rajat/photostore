import type { Album, Member, Person, Photo, Place, Suggestion, ThingTag } from './types';
import type { SwatchKey } from './types';

// Seed data for the prototype. Generated once at module load so it stays
// stable across renders. Everything is fake — no backend, no real images.

const SWATCHES: SwatchKey[] = ['s1', 's2', 's3', 's4', 's5', 's6', 's7', 's8'];
const pick = <T,>(arr: T[], i: number): T => arr[i % arr.length];

export const PLACES: Place[] = [
    { id: 'lisbon', name: 'Lisbon', swatch: 's2' },
    { id: 'porto', name: 'Porto', swatch: 's5' },
    { id: 'home', name: 'Home', swatch: 's6' },
    { id: 'goa', name: 'Goa', swatch: 's7' },
    { id: 'tokyo', name: 'Tokyo', swatch: 's4' },
];

export const THINGS: ThingTag[] = [
    { id: 'beach', name: 'Beach', count: 61, swatch: 's2' },
    { id: 'birthday', name: 'Birthday', count: 18, swatch: 's3' },
    { id: 'dog', name: 'Dog', count: 94, swatch: 's7' },
    { id: 'sunsets', name: 'Sunsets', count: 42, swatch: 's5' },
    { id: 'food', name: 'Food', count: 77, swatch: 's8' },
    { id: 'mountains', name: 'Mountains', count: 33, swatch: 's6' },
    { id: 'city', name: 'City', count: 120, swatch: 's4' },
    { id: 'flowers', name: 'Flowers', count: 25, swatch: 's1' },
];

const PLACE_IDS = PLACES.map((p) => p.id);
const THING_NAMES = THINGS.map((t) => t.name.toLowerCase());
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep'];

// People (some named, some unnamed clusters).
export const PEOPLE: Person[] = [
    { id: 'priya', name: 'Priya', swatch: 's6', photoIds: [] },
    { id: 'liam', name: 'Liam', swatch: 's2', photoIds: [] },
    { id: 'mom', name: 'Mom', swatch: 's3', photoIds: [] },
    { id: 'sam', name: 'Sam', swatch: 's5', photoIds: [] },
    { id: 'u1', name: null, swatch: 's4', photoIds: [] },
    { id: 'u2', name: null, swatch: 's7', photoIds: [] },
];

// 48 photos, deterministically spread across people / places / tags / years.
export const PHOTOS: Photo[] = Array.from({ length: 48 }, (_, i) => {
    const swatch = pick(SWATCHES, i);
    const year = 2020 + (i % 4);
    const placeId = i % 5 === 0 ? null : pick(PLACE_IDS, i);
    const personIds: string[] = [];
    if (i % 2 === 0) personIds.push(pick(PEOPLE, i).id);
    if (i % 3 === 0) personIds.push(pick(PEOPLE, i + 2).id);
    const tags = [pick(THING_NAMES, i)];
    if (i % 4 === 0) tags.push(pick(THING_NAMES, i + 3));
    return {
        id: `p${i}`,
        filename: `IMG_${String(4000 + i)}.${i % 6 === 0 ? 'CR3' : 'HEIC'}`,
        swatch,
        dateLabel: `${pick(MONTHS, i)} ${1 + (i % 27)}, ${year}`,
        year,
        rating: i % 5 === 0 ? 5 : i % 3 === 0 ? 4 : 0,
        liked: i % 7 === 0,
        placeId,
        personIds: Array.from(new Set(personIds)),
        tags: Array.from(new Set(tags)),
    };
});

// Back-fill each person's photoIds from the assignments above.
for (const person of PEOPLE) {
    person.photoIds = PHOTOS.filter((p) => p.personIds.includes(person.id)).map((p) => p.id);
}
// Guarantee every person has a handful so the grids look real.
for (const person of PEOPLE) {
    if (person.photoIds.length < 4) {
        person.photoIds = PHOTOS.slice(0, 6).map((p) => p.id);
    }
}

export const ALBUMS: Album[] = [
    {
        id: 'algarve',
        name: 'Algarve, 2022',
        coverPhotoId: 'p2',
        photoIds: PHOTOS.slice(0, 12).map((p) => p.id),
        share: { isPublic: true, expiry: '7', code: '4F2A' },
    },
    {
        id: 'reunion',
        name: 'Family Reunion',
        coverPhotoId: 'p6',
        photoIds: PHOTOS.slice(6, 20).map((p) => p.id),
        share: { isPublic: false, expiry: '7', code: '9K3P' },
    },
    {
        id: 'dog',
        name: 'Dog: the sequel',
        coverPhotoId: 'p3',
        photoIds: PHOTOS.slice(12, 22).map((p) => p.id),
        share: { isPublic: false, expiry: '30', code: 'B7QX' },
    },
    {
        id: 'screens',
        name: 'Screenshots I keep',
        coverPhotoId: 'p1',
        photoIds: PHOTOS.slice(20, 28).map((p) => p.id),
        share: { isPublic: false, expiry: '7', code: 'M2W6' },
    },
];

export const MEMBERS: Member[] = [
    { id: 'me', name: 'Rajat Verma', sub: 'rajat@example.com', initials: 'RV', color: 'linear-gradient(135deg,#3f5a73,#6fa3c9)', role: 'owner' },
    { id: 'priya', name: 'Priya S.', sub: 'Member · joined Sep 12', initials: 'PS', color: 'linear-gradient(135deg,#7a4a4f,#c98f92)', role: 'contribute' },
    { id: 'rohan', name: 'rohan@example.com', sub: 'Invited 2 days ago', initials: '?', color: '', role: 'view', pending: true },
];

export const SUGGESTIONS: Suggestion[] = [
    { id: 'trip', text: 'Trip to Lisbon · Mar 14–16 · 42 photos', action: 'Create album', target: { page: 'explore' } },
    { id: 'unnamed', text: '40 photos of someone you haven’t named', action: 'Name them', target: { page: 'people' } },
    { id: 'onthisday', text: 'On this day, 2023', action: 'View', target: { page: 'gallery' } },
];

export const QUEUE_STAGES = [
    { name: 'Thumbnails', wait: 4, run: 1, noData: 0, fail: 0 },
    { name: 'EXIF', wait: 2, run: 0, noData: 0, fail: 0 },
    { name: 'OCR', wait: 11, run: 2, noData: 3, fail: 0 },
    { name: 'AI Vision', wait: 6, run: 1, noData: 0, fail: 0 },
    { name: 'Map tagging', wait: 0, run: 0, noData: 0, fail: 1 },
    { name: 'Faces', wait: 3, run: 0, noData: 0, fail: 0 },
];

export const ALL_SWATCHES = SWATCHES;
