import React from 'react';

type Variant = 'default' | 'primary' | 'danger' | 'soft' | 'link';
type Size = 'md' | 'sm';

const VARIANT_CLASS: Record<Variant, string> = {
    default: '',
    primary: 'btn-primary',
    danger: 'btn-danger',
    soft: 'btn-soft',
    link: 'btn-link',
};

const classes = (...parts: (string | false | undefined)[]) => parts.filter(Boolean).join(' ');

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
    variant?: Variant;
    /** md is the 44px default for main actions; sm is the 34px compact size. */
    size?: Size;
}

/** The one button: variants and sizes map onto the shared .btn classes. */
export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
    ({ variant = 'default', size = 'md', className, type = 'button', ...rest }, ref) => (
        <button
            ref={ref}
            type={type}
            className={classes('btn', VARIANT_CLASS[variant], size === 'sm' && 'btn-sm', className)}
            {...rest}
        />
    ),
);
Button.displayName = 'Button';

interface IconButtonProps extends Omit<ButtonProps, 'aria-label' | 'children'> {
    /** Required: an icon alone says nothing to a screen reader. Also shown as the tooltip. */
    label: string;
    children: React.ReactNode;
}

/** Icon-only round button. */
export const IconButton = React.forwardRef<HTMLButtonElement, IconButtonProps>(
    ({ label, variant = 'soft', size = 'md', className, type = 'button', ...rest }, ref) => (
        <button
            ref={ref}
            type={type}
            aria-label={label}
            title={label}
            className={classes('btn', 'btn-icon', VARIANT_CLASS[variant], size === 'sm' && 'btn-sm', className)}
            {...rest}
        />
    ),
);
IconButton.displayName = 'IconButton';

interface ChipProps extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'aria-pressed'> {
    selected?: boolean;
}

/** Filter chip: a toggle that reports its state through aria-pressed. */
export const Chip: React.FC<ChipProps> = ({ selected = false, className, type = 'button', ...rest }) => (
    <button type={type} aria-pressed={selected} className={classes('chip', className)} {...rest} />
);
