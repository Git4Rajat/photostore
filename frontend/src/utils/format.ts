// Human-friendly count formatting so UI copy never reads "5 photo(s)".
//   pluralWord(1, 'photo') => "photo",  pluralWord(3, 'photo') => "photos"
//   plural(1, 'photo')     => "1 photo", plural(3, 'photo')    => "3 photos"
const pluralWord = (count: number, noun: string, suffix = 's'): string => (
    count === 1 ? noun : `${noun}${suffix}`
);

export const plural = (count: number, noun: string, suffix = 's'): string => (
    `${count} ${pluralWord(count, noun, suffix)}`
);

// Two-letter avatar initials from a name or an email. An email's domain goes
// first, so rajatverma57@gmail.com gives "RA", not "RG".
export const initialsFor = (value: string): string => {
    const local = value.trim().replace(/@.*/, '');
    const parts = local.split(/[\s._-]+/).filter(Boolean);
    const letters = parts.length >= 2 ? parts[0][0] + parts[1][0] : local.slice(0, 2);
    return (letters || '?').toUpperCase();
};
