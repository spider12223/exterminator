local PRICES = table.freeze({
	sword = 100,
	shield = 80,
})

local Shop = {}

function Shop.priceOf(item: string): number?
	return PRICES[item]
end

return Shop
