"""
UI Display Module - Hyperliquid Style
Renders the terminal UI to match the Hyperliquid copy terminal design
"""

from datetime import datetime
from rich.layout import Layout
from rich.table import Table
from rich.text import Text
from rich import box


def create_layout() -> Layout:
    """Create the terminal layout matching Hyperliquid design"""
    layout = Layout()
    
    # Main structure: header, main content, activity log, footer
    layout.split_column(
        Layout(name="header", size=1),
        Layout(name="main"),
        Layout(name="activity_log", size=15),
        Layout(name="footer", size=1)
    )
    
    # Main area: left (positions tables) and right (stats panel)
    layout["main"].split_row(
        Layout(name="positions", ratio=2),
        Layout(name="stats_panel", ratio=1)
    )
    
    # Left side: target positions and our positions
    layout["positions"].split_column(
        Layout(name="target_positions"),
        Layout(name="our_positions")
    )
    
    return layout


def render_header(terminal) -> Text:
    """Render header bar"""
    target_short = terminal.config['hyperliquid_target_wallet'][:10] + "..."
    status_text = "[green]ACTIVE[/green]" if terminal.is_running else "[red]PAUSED[/red]"
    
    return Text(
        f"EXTENDED DEX COPY TERMINAL | Status: {status_text} | Target: {target_short}",
        justify="left",
        style="bold cyan"
    )


def render_target_positions(terminal) -> Table:
    """Render target wallet positions table"""
    table = Table(
        title="Target Wallet Positions",
        box=box.SQUARE,
        border_style="cyan",
        show_header=True,
        header_style="bold white",
        padding=(0, 1),
        expand=True
    )
    
    table.add_column("Asset", style="white", width=10)
    table.add_column("Size", justify="right", style="white", width=14)
    table.add_column("Entry", justify="right", style="white", width=10)
    table.add_column("Value", justify="right", style="white", width=12)
    table.add_column("PnL", justify="right", style="white", width=12)
    
    # Get target positions
    target_positions = terminal.position_tracker.target_positions if terminal.position_tracker else {}
    
    if target_positions:
        for coin, pos_info in sorted(target_positions.items()):
            size = pos_info.get('size', 0)
            entry_px = pos_info.get('entry_price', 0)
            position_value = pos_info.get('notional_value', 0)
            unrealized_pnl = pos_info.get('unrealized_pnl', 0)
            
            # Format size
            if size > 0:
                size_str = f"{size:,.4f}"
            else:
                size_str = f"{size:,.4f}"
            
            # Format entry and value
            entry_str = f"${entry_px:.2f}"
            value_str = f"${abs(position_value):,.2f}"
            
            # Format PnL with color
            if unrealized_pnl > 0:
                pnl_str = f"[green]${unrealized_pnl:,.2f}[/green]"
            elif unrealized_pnl < 0:
                pnl_str = f"[red]-${abs(unrealized_pnl):,.2f}[/red]"
            else:
                pnl_str = "$0.00"
            
            table.add_row(coin, size_str, entry_str, value_str, pnl_str)
    else:
        table.add_row("[dim]No positions[/dim]", "", "", "", "")
    
    return table


def render_our_positions(terminal) -> Table:
    """Render our positions table"""
    table = Table(
        title="Our Positions",
        box=box.SQUARE,
        border_style="green",
        show_header=True,
        header_style="bold white",
        padding=(0, 1),
        expand=True
    )
    
    table.add_column("Asset", style="white", width=10)
    table.add_column("Size", justify="right", style="white", width=14)
    table.add_column("Entry", justify="right", style="white", width=10)
    table.add_column("Value", justify="right", style="white", width=12)
    table.add_column("PnL", justify="right", style="white", width=12)
    
    # Get our positions
    our_positions = getattr(terminal, 'our_extended_positions', {})
    
    if our_positions:
        for coin, pos_info in sorted(our_positions.items()):
            size = pos_info.get('size', 0)
            entry_px = pos_info.get('entry_px', 0)
            mark_price = pos_info.get('mark_price', 0)
            position_value = pos_info.get('position_value', 0)
            unrealized_pnl = pos_info.get('unrealized_pnl', 0)
            
            # Format size with appropriate decimals
            abs_size = abs(size)
            if abs_size >= 100:
                size_str = f"{size:,.1f}"
            elif abs_size >= 1:
                size_str = f"{size:,.2f}"
            else:
                size_str = f"{size:,.4f}"
            
            # Format entry price with appropriate decimals based on price magnitude
            if entry_px >= 100:
                entry_str = f"${entry_px:,.2f}"
            elif entry_px >= 1:
                entry_str = f"${entry_px:.4f}"
            elif entry_px > 0:
                entry_str = f"${entry_px:.6f}"
            else:
                entry_str = "$0.00"
            
            # Format value
            value_str = f"${abs(position_value):,.2f}"
            
            # Format PnL with color
            if unrealized_pnl > 0:
                pnl_str = f"[green]+${unrealized_pnl:,.2f}[/green]"
            elif unrealized_pnl < 0:
                pnl_str = f"[red]-${abs(unrealized_pnl):,.2f}[/red]"
            else:
                pnl_str = "$0.00"
            
            table.add_row(coin, size_str, entry_str, value_str, pnl_str)
    else:
        table.add_row("[dim]No positions[/dim]", "", "", "", "")
    
    return table


def render_stats_panel(terminal) -> Text:
    """Render statistics panel on the right side"""
    stats = Text()
    
    # Our Account section
    stats.append("Our Account\n", style="bold cyan")
    our_account_value = getattr(terminal, 'our_account_value', 0.0)
    stats.append(f"Account Value    ${our_account_value:,.2f}\n", style="white")
    
    our_unrealized_pnl = getattr(terminal, 'our_unrealized_pnl', 0.0)
    if our_unrealized_pnl > 0:
        stats.append(f"Unrealized PnL   ", style="white")
        stats.append(f"+${our_unrealized_pnl:,.2f}\n", style="green")
    elif our_unrealized_pnl < 0:
        stats.append(f"Unrealized PnL   ", style="white")
        stats.append(f"-${abs(our_unrealized_pnl):,.2f}\n", style="red")
    else:
        stats.append(f"Unrealized PnL   $0.00\n", style="white")
    
    our_margin_used = getattr(terminal, 'our_margin_used', 0.0)
    stats.append(f"Margin Used      ${our_margin_used:,.2f}\n", style="white")
    
    # Get fees from trade_executor (calculated on each trade) or session tracking
    fees_paid = 0.0
    if hasattr(terminal, 'trade_executor') and terminal.trade_executor:
        fees_paid = getattr(terminal.trade_executor, 'total_fees_paid', 0.0)
    if fees_paid == 0:
        fees_paid = getattr(terminal, 'session_fees_paid', 0.0)
    
    stats.append(f"Fees Paid        ", style="white")
    # Show more decimals for small fees
    if fees_paid < 0.01:
        stats.append(f"-${fees_paid:.4f}\n", style="red")
    else:
        stats.append(f"-${fees_paid:.2f}\n", style="red")
    
    stats.append("\n")
    
    # Target Account section
    stats.append("Target Account\n", style="bold yellow")
    target_account_value = getattr(terminal, 'target_account_value', 0.0)
    stats.append(f"Account Value    ${target_account_value:,.2f}\n", style="white")
    
    target_unrealized_pnl = getattr(terminal, 'target_unrealized_pnl', 0.0)
    if target_unrealized_pnl > 0:
        stats.append(f"Unrealized PnL   ", style="white")
        stats.append(f"+${target_unrealized_pnl:,.2f}\n", style="green")
    elif target_unrealized_pnl < 0:
        stats.append(f"Unrealized PnL   ", style="white")
        stats.append(f"-${abs(target_unrealized_pnl):,.2f}\n", style="red")
    else:
        stats.append(f"Unrealized PnL   $0.00\n", style="white")
    
    target_margin_used = getattr(terminal, 'target_margin_used', 0.0)
    stats.append(f"Margin Used      ${target_margin_used:,.2f}\n", style="white")
    
    stats.append("\n")
    
    # Trading Stats section
    stats.append("Trading Stats\n", style="bold green")
    stats.append(f"Total Trades     {terminal.total_trades_copied}\n", style="white")
    stats.append(f"Successful       ", style="white")
    stats.append(f"{terminal.successful_copies}\n", style="green")
    stats.append(f"Failed           ", style="white")
    stats.append(f"{terminal.failed_copies}\n", style="red")
    
    if terminal.total_trades_copied > 0:
        success_rate = (terminal.successful_copies / terminal.total_trades_copied) * 100
        stats.append(f"Success Rate     {success_rate:.1f}%\n", style="white")
    else:
        stats.append(f"Success Rate     0%\n", style="white")
    
    stats.append(f"Avg Latency      {terminal.avg_latency_ms:.0f}ms\n", style="white")
    
    if terminal.websocket_connected:
        stats.append(f"HL WebSocket     ", style="white")
        stats.append("Connected\n", style="green")
    else:
        stats.append(f"HL WebSocket     ", style="white")
        stats.append("Disconnected\n", style="yellow")
    
    # Extended WebSocket status
    if hasattr(terminal, 'extended_ws') and terminal.extended_ws:
        ws = terminal.extended_ws
        if ws.connected and hasattr(ws, 'message_count') and ws.message_count > 0:
            stats.append(f"Ext WebSocket    ", style="white")
            # Calculate messages per second if we have timing info
            rate_str = ""
            if hasattr(ws, '_last_msg_time') and hasattr(ws, '_msg_start_time'):
                import time
                elapsed = time.time() - ws._msg_start_time
                if elapsed > 0:
                    rate = ws.message_count / elapsed
                    rate_str = f" ~{rate:.1f}/s"
            stats.append(f"Live ({ws.message_count}{rate_str})\n", style="green")
        elif ws.connected:
            stats.append(f"Ext WebSocket    ", style="white")
            stats.append("Connecting...\n", style="yellow")
        else:
            stats.append(f"Ext WebSocket    ", style="white")
            stats.append("Polling\n", style="yellow")
    else:
        stats.append(f"Ext WebSocket    ", style="white")
        stats.append("Polling\n", style="yellow")
    
    if hasattr(terminal, 'last_check_time') and terminal.last_check_time:
        import time
        elapsed = time.time() - terminal.last_check_time
        stats.append(f"Last Check       {elapsed:.1f}s ago\n", style="white")
    
    stats.append("\n")
    
    # Capital Management section
    stats.append("Capital Mgmt\n", style="bold magenta")
    mode = terminal.config.get('capital_mode', 'fixed').title()
    stats.append(f"Mode             {mode}\n", style="white")
    
    if terminal.config.get('capital_mode') == 'mirror':
        stats.append(f"Strategy         ", style="white")
        stats.append("1:1 Copy (Mirror)\n", style="cyan")
    else:
        multiplier = terminal.config.get('capital_multiplier', 1.0)
        stats.append(f"Strategy         {multiplier}x\n", style="white")
    
    stats.append(f"Account Value    ${our_account_value:,.2f}\n", style="white")
    stats.append(f"Margin Used      ${our_margin_used:,.2f}\n", style="white")
    
    available = our_account_value - our_margin_used
    stats.append(f"Available        ", style="white")
    stats.append(f"${available:,.2f}\n", style="green")
    
    if our_account_value > 0:
        margin_usage_pct = (our_margin_used / our_account_value) * 100
        if margin_usage_pct > 75:
            color = "red"
        elif margin_usage_pct > 50:
            color = "yellow"
        else:
            color = "green"
        stats.append(f"Margin Usage     ", style="white")
        stats.append(f"{margin_usage_pct:.1f}%\n", style=color)
    else:
        stats.append(f"Margin Usage     0%\n", style="white")
    
    return stats


def render_activity_log(terminal) -> Text:
    """Render activity log"""
    log_text = Text()
    
    # Get recent activity (last 20 entries)
    activity_log = getattr(terminal, 'activity_log', [])
    recent_activity = activity_log[-20:] if len(activity_log) > 20 else activity_log
    
    if not recent_activity:
        log_text.append("No activity yet...", style="dim")
        return log_text
    
    for entry in recent_activity:
        timestamp = entry.get('timestamp', '')
        action = entry.get('action', '')
        coin = entry.get('coin', '')
        details = entry.get('details', '')
        status = entry.get('status', 'info')
        
        # Format timestamp
        log_text.append(timestamp, style="dim")
        log_text.append(" ", style="dim")
        
        # Color based on status
        if status == 'success':
            style = "green"
        elif status == 'error':
            style = "red"
        elif status == 'warning':
            style = "yellow"
        else:
            style = "white"
        
        # Format message
        message = f"{action} {coin}: {details}" if coin else f"{action}: {details}"
        log_text.append(message, style=style)
        log_text.append("\n")
    
    return log_text


def render_footer(terminal) -> Text:
    """Render footer with controls and error"""
    footer = Text()
    
    footer.append("Controls: ", style="bold cyan")
    footer.append("[Q] Quit | [P] Pause/Resume | [R] Reload Config", style="cyan")
    footer.append(" | ", style="dim")
    
    if hasattr(terminal, 'last_error') and terminal.last_error:
        footer.append("Last Error: ", style="bold red")
        footer.append(terminal.last_error, style="red")
    else:
        footer.append("No errors", style="green")
    
    return footer


def update_display(layout: Layout, terminal):
    """Update the entire display"""
    # Header
    layout["header"].update(render_header(terminal))
    
    # Position tables
    layout["target_positions"].update(render_target_positions(terminal))
    layout["our_positions"].update(render_our_positions(terminal))
    
    # Stats panel
    from rich.panel import Panel
    stats_panel = Panel(
        render_stats_panel(terminal),
        title="Statistics",
        border_style="cyan",
        padding=(1, 2)
    )
    layout["stats_panel"].update(stats_panel)
    
    # Activity log
    activity_panel = Panel(
        render_activity_log(terminal),
        title="Activity Log",
        border_style="yellow",
        padding=(0, 1)
    )
    layout["activity_log"].update(activity_panel)
    
    # Footer
    layout["footer"].update(render_footer(terminal))
