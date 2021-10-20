import argparse
import csv
from collections import defaultdict

import numpy as np
import plotly.graph_objects as go


def process_runlog(filename):
    """Read results from file.

    Parameters:
        filename: str

    Returns: dict
        key: 2-tuple of int
            (steps, pressed)
        value: list of float
            list of results, sorted in descending order; -inf if fault
    """
    data = defaultdict(list)
    with open(filename) as f:
        reader = csv.reader(f)
        for line in reader:
            run = line[1].strip().split(',')
            steps, pressed = int(run[2]), int(run[3])

            result = line[2].strip()
            if result == 'fault':
                result = -np.inf
            else:
                result = float(result)

            data[(steps, pressed)].append(result)

    data = dict(data)

    # Sort results
    for val_l in data.values():
        val_l.sort(reverse=True)

    return data


def plot_data(data):
    """
    Parameters:
        data: dict
            key: 2-tuple of int
                (steps, pressed)
            value: list of float
                sorted list of results; -inf if fault

    Returns: None
    """
    # Find steps/pressed range
    steps_min, steps_max, pressed_min, pressed_max = 1_000, 0, 1_000, 0
    for steps, pressed in data.keys():
        steps_min = min(steps_min, steps)
        steps_max = max(steps_max, steps)
        pressed_min = min(pressed_min, pressed)
        pressed_max = max(pressed_max, pressed)
    num_steps, num_pressed = steps_max - steps_min + 1, pressed_max - pressed_min + 1

    # Calculate heatmap data
    heatmap_data = np.zeros((num_steps, num_pressed))
    heatmap_data.fill(np.nan)
    for (steps, pressed), val_l in data.items():
        heatmap_data[steps-steps_min, pressed-pressed_min] = val_l[0]

    # Find failed
    failed_steps, failed_pressed = [], []
    for (steps, pressed), val_l in data.items():
        if np.isneginf(val_l).all():
            failed_steps.append(steps)
            failed_pressed.append(pressed)

    # Create tooltip texts
    tooltip_texts = []
    for pressed in range(pressed_min, pressed_max+1):
        this_line = []
        for steps in range(steps_min, steps_max+1):
            text = f'<b>steps: </b>{steps}<br /><b>pressed: </b>{pressed}<br />'
            results = data.get((steps, pressed))
            if results is None:
                text += 'no results'
            else:
                text += f'<b>results: </b>' + ', '.join(['fault' if np.isneginf(res) else f'{res:.2f}' for res in results])

            this_line.append(text)
        tooltip_texts.append(this_line)

    # Plot heatmap
    common_axis_settings = {
        # Ticks
        'ticks': 'outside',
        'ticklen': 10,
        'tickwidth': 2,
        'tickfont': {
            'size': 20,
        },
        # Spines
        'showline': True,
        'linecolor': 'grey',
        'mirror': True,
        # Grid
        'gridcolor': 'lightgrey',
    }
    figure = go.Figure(
        data=go.Heatmap(
            x=list(range(steps_min, steps_max+1)),
            y=list(range(pressed_min, pressed_max+1)),
            z=heatmap_data.T,
            zmin=80.,
            zmax=102.,
            colorscale='jet',
            hoverinfo='text',
            hovertext=tooltip_texts,
            hoverlabel={
                # 'bgcolor': 'white',
                'font_size': 16,
            },
        ),
        layout={
            'plot_bgcolor': 'white',
            'margin': {
                't': 70,
                'l': 90,
            },
            'title': {
                'text': 'Throw results [m]',
                'font': {
                    'size': 30,
                },
                'xanchor': 'center',
                'x': 0.5,
            },
            'xaxis': {
                'title': {
                    'text': 'num steps',
                    'font': {
                        'size': 25,
                    },
                },
                'range': (steps_min-0.5, steps_max+0.5),
                **common_axis_settings,
                # 'scaleanchor': 'y',
            },
            'yaxis': {
                'title': {
                    'text': 'time pressed [ms]',
                    'font': {
                        'size': 25,
                    },
                },
                'range': (pressed_min-0.5, pressed_max+0.5),
                **common_axis_settings,
            },
        }
    )

    # Plot cross markers
    figure.add_trace(
        go.Scatter(
            x=failed_steps,
            y=failed_pressed,
            mode='markers',
            marker={
                'symbol': 'x-thin',
                'color': 'black',
                'size': 8,
                'line_width': 3,
            },
            hoverinfo='skip',
        )
    )

    figure.show()


def main():
    parser = argparse.ArgumentParser(description='Show results from runlog')
    parser.add_argument('runlog_filename', help='Runlog file')

    args = parser.parse_args()

    data = process_runlog(args.runlog_filename)

    plot_data(data)


if __name__ == '__main__':
    main()
