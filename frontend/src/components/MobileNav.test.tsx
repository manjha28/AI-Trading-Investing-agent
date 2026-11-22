import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MobileNav } from './MobileNav';

afterEach(cleanup);

describe('MobileNav', () => {
  it('is one row of at most five items, with the rest in a More sheet that has the Dhan connection and Sign out', () => {
    const setActiveView = vi.fn();
    const onSignOut = vi.fn();
    render(<MobileNav activeView="overview" setActiveView={setActiveView} onSignOut={onSignOut} />);
    const nav = screen.getByRole('navigation', { name: 'Workspace' });
    expect(nav.querySelectorAll('button').length).toBe(5);
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /More/ }));
    expect(screen.getByRole('dialog', { name: 'More' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /Dhan connection/ }));
    expect(setActiveView).toHaveBeenCalledWith('brokers');
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /More/ }));
    fireEvent.click(screen.getByRole('button', { name: /Sign out/ }));
    expect(onSignOut).toHaveBeenCalled();
  });
  it('marks More active when a view that lives in it is open', () => {
    render(<MobileNav activeView="journal" setActiveView={vi.fn()} onSignOut={vi.fn()} />);
    expect(screen.getByRole('button', { name: /More/ }).className).toContain('active');
  });
});
