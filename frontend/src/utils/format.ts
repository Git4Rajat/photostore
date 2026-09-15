// Human-friendly count formatting so UI copy never reads "5 photo(s)".
//   pluralWord(1, 'photo') => "photo",  pluralWord(3, 'photo') => "photos"
//   plural(1, 'photo')     => "1 photo", plural(3, 'photo')    => "3 photos"
export const pluralWord = (count: number, noun: string, suffix = 's'): string => (
    count === 1 ? noun : `${noun}${suffix}`
);

export const plural = (count: number, noun: string, suffix = 's'): string => (
    `${count} ${pluralWord(count, noun, suffix)}`
);
