local function defaults()
	return {
		volume = 0.5,
		keybinds = {},
	}
end

local Settings = {}

function Settings.new()
	return defaults()
end

function Settings.bind(settings, action: string, key: string)
	settings.keybinds[action] = key
end

return Settings
