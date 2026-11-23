import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { BrandLockup } from './BrandLockup';

describe('BrandLockup', () => {
  it('shows the FinByte Nexus wordmark with the logo tile', () => {
    render(<BrandLockup />);
    expect(screen.getByRole('img', { name: 'FinByte Nexus' })).toBeTruthy();
    expect(screen.getByText('FinByte')).toBeTruthy();
    expect(screen.getByText('NEXUS')).toBeTruthy();
    expect(document.querySelector('img[src="/brand/icon-128.png"]')).toBeTruthy();
  });

  it('adds the tagline and capability chips on the sign-in screen', () => {
    render(<BrandLockup tagline />);
    expect(screen.getByText('Intelligence Beyond the Trade.')).toBeTruthy();
    for (const chip of ['AI Research', 'Quant Analytics', 'Market Intelligence']) expect(screen.getByText(chip)).toBeTruthy();
  });
});
