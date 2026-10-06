-- Per-player bridge, loaded by the launcher. No account credentials or HTTP in mpv.
local mp = require 'mp'
local utils = require 'mp.utils'
local last = nil

local function snapshot(new_file)
    local path = mp.get_property('path')
    if not path then return nil end
    return {path = path, position = mp.get_property_number('time-pos'),
            duration = mp.get_property_number('duration'), new_file = new_file or false}
end

local function send(name, sample)
    if sample then mp.commandv('script-message', name, utils.format_json(sample)) end
end

local function sample(new_file)
    last = snapshot(new_file)
    send('serializd-sample', last)
end

mp.add_periodic_timer(1, function() sample(false) end)
mp.register_event('file-loaded', function() sample(true) end)
mp.register_event('end-file', function()
    -- Preserve the last observed position; EOF alone never implies watched.
    if last then
        last.new_file = false
        send('serializd-sample', last)
    end
    last = nil
end)
-- Binding names must differ from broadcast names to avoid invoking the binding again.
mp.add_key_binding('Ctrl+Shift+l', 'serializd-mark-key', function()
    send('serializd-mark', snapshot(false))
end)
mp.add_key_binding('Ctrl+Shift+u', 'serializd-undo-key', function()
    send('serializd-undo', snapshot(false))
end)
