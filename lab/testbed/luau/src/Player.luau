--!strict
local DEFAULTS = {
	coins = 0,
	inventory = {} :: { string },
}

local Player = {}
Player.__index = Player

export type Player = typeof(setmetatable({} :: { name: string, coins: number, inventory: { string } }, Player))

function Player.new(name: string): Player
	local self = setmetatable({} :: any, Player)
	self.name = name
	self.coins = DEFAULTS.coins
	self.inventory = DEFAULTS.inventory
	return self
end

function Player.addItem(self: Player, item: string)
	table.insert(self.inventory, item)
end

function Player.hasItem(self: Player, item: string): boolean
	return table.find(self.inventory, item) ~= nil
end

return Player
