import React from 'react'
import { useActions, useValues } from 'kea'
import {
    PLAYBACK_SPEEDS,
    sessionRecordingPlayerLogic,
} from 'scenes/session-recordings/player/sessionRecordingPlayerLogic'
import { Select, Tooltip } from 'antd'
import { SessionPlayerState } from '~/types'
import { IconPause, IconPlay, IconSeekBack, IconSeekForward } from 'scenes/session-recordings/player/icons'
import { Slider } from 'scenes/session-recordings/player/Slider'
import { colonDelimitedDuration } from 'lib/utils'

interface PlayerControllerProps {
    toggleFullScreen: () => void
}

export function PlayerController({}: PlayerControllerProps): JSX.Element {
    const { togglePlayPause, seek, seekBackward, seekForward, setSpeed } = useActions(sessionRecordingPlayerLogic)
    const { currentPlayerState, jumpTimeMs, meta, time, speed } = useValues(sessionRecordingPlayerLogic)

    return (
        <div className="rrweb-controller">
            <div>
                <span>
                    {currentPlayerState === SessionPlayerState.PLAY ||
                    currentPlayerState === SessionPlayerState.SKIP ? (
                        <IconPause
                            onClick={togglePlayPause}
                            className="rrweb-controller-icon ph-rrweb-controller-icon-play-pause"
                        />
                    ) : (
                        <IconPlay
                            onClick={togglePlayPause}
                            className="rrweb-controller-icon ph-rrweb-controller-icon-play-pause"
                        />
                    )}
                </span>
                <Tooltip
                    placement="top"
                    overlayInnerStyle={{ minHeight: 'auto' }}
                    overlay={`Back ${jumpTimeMs / 1000}s (← left arrow)`}
                >
                    <span>
                        <IconSeekBack onClick={seekBackward} time={jumpTimeMs / 1000} />
                    </span>
                </Tooltip>
                <Tooltip
                    placement="top"
                    overlayInnerStyle={{ minHeight: 'auto' }}
                    overlay={`Forward ${jumpTimeMs / 1000}s (→ right arrow)`}
                >
                    <span>
                        <IconSeekForward onClick={seekForward} time={jumpTimeMs / 1000} />
                    </span>
                </Tooltip>
                <div className="rrweb-timestamp">
                    {colonDelimitedDuration(time.current / 1000)} / {colonDelimitedDuration(meta.totalTime / 1000)}
                </div>
            </div>
            <div className="rrweb-progress">
                <Slider value={time.current} total={meta.totalTime} onChange={seek} />
            </div>
            <div style={{ justifyContent: 'flex-end' }}>
                <Select
                    onChange={(nextSpeed: number) => setSpeed(nextSpeed)}
                    value={speed}
                    dropdownMatchSelectWidth={false}
                    size="small"
                >
                    <Select.OptGroup label="Speed">
                        {PLAYBACK_SPEEDS.map((speedToggle) => (
                            <Select.Option key={speedToggle} value={speedToggle}>
                                <span
                                    className="rrweb-speed-toggle"
                                    // style={{
                                    //     fontWeight: speedToggle === speed ? 'bold' : 'normal',
                                    // }}
                                >
                                    {speedToggle}x
                                </span>
                            </Select.Option>
                        ))}
                    </Select.OptGroup>
                </Select>
            </div>
        </div>
    )
}
