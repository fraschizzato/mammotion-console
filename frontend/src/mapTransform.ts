import type { FeatureCollection, Geometry, GeometryCollection, Position } from "geojson";

export type MapCalibration = {
  rotation_degrees?: number;
  offset_east_m?: number;
  offset_north_m?: number;
  origin_latitude?: number | null;
  origin_longitude?: number | null;
};

export type MapTransformOrigin = { latitude: number; longitude: number };

const METRES_PER_LATITUDE_DEGREE = 111_320;

function finite(value: unknown, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export function resolveMapTransformOrigin(
  calibration: MapCalibration | undefined,
  bounds: number[] | undefined,
  fallback: MapTransformOrigin | null | undefined,
): MapTransformOrigin | null {
  const latitude = calibration?.origin_latitude;
  const longitude = calibration?.origin_longitude;
  if (Number.isFinite(latitude) && Number.isFinite(longitude)) {
    return { latitude: Number(latitude), longitude: Number(longitude) };
  }
  if (bounds?.length === 4 && bounds.every(Number.isFinite)) {
    return {
      longitude: (bounds[0] + bounds[2]) / 2,
      latitude: (bounds[1] + bounds[3]) / 2,
    };
  }
  return fallback || null;
}

export function transformMapPosition(
  position: Position,
  calibration: MapCalibration | undefined,
  origin: MapTransformOrigin | null,
): Position {
  if (!origin) return [...position];
  const longitude = finite(position[0], NaN);
  const latitude = finite(position[1], NaN);
  if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) return [...position];
  const latitudeScale = METRES_PER_LATITUDE_DEGREE;
  const longitudeScale = latitudeScale * Math.cos((origin.latitude * Math.PI) / 180);
  if (!Number.isFinite(longitudeScale) || Math.abs(longitudeScale) < 1e-9) {
    return [...position];
  }
  const radians = (finite(calibration?.rotation_degrees) * Math.PI) / 180;
  const east = (longitude - origin.longitude) * longitudeScale;
  const north = (latitude - origin.latitude) * latitudeScale;
  const transformedEast =
    Math.cos(radians) * east - Math.sin(radians) * north + finite(calibration?.offset_east_m);
  const transformedNorth =
    Math.sin(radians) * east + Math.cos(radians) * north + finite(calibration?.offset_north_m);
  return [
    origin.longitude + transformedEast / longitudeScale,
    origin.latitude + transformedNorth / latitudeScale,
    ...position.slice(2),
  ];
}

export function inverseMapPosition(
  position: Position,
  calibration: MapCalibration | undefined,
  origin: MapTransformOrigin | null,
): Position {
  if (!origin) return [...position];
  const longitude = finite(position[0], NaN);
  const latitude = finite(position[1], NaN);
  if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) return [...position];
  const latitudeScale = METRES_PER_LATITUDE_DEGREE;
  const longitudeScale = latitudeScale * Math.cos((origin.latitude * Math.PI) / 180);
  if (!Number.isFinite(longitudeScale) || Math.abs(longitudeScale) < 1e-9) {
    return [...position];
  }
  const radians = (finite(calibration?.rotation_degrees) * Math.PI) / 180;
  const east = (longitude - origin.longitude) * longitudeScale - finite(calibration?.offset_east_m);
  const north = (latitude - origin.latitude) * latitudeScale - finite(calibration?.offset_north_m);
  return [
    origin.longitude + (Math.cos(radians) * east + Math.sin(radians) * north) / longitudeScale,
    origin.latitude + (-Math.sin(radians) * east + Math.cos(radians) * north) / latitudeScale,
    ...position.slice(2),
  ];
}

function transformCoordinates(
  coordinates: unknown,
  calibration: MapCalibration | undefined,
  origin: MapTransformOrigin | null,
): unknown {
  if (!Array.isArray(coordinates)) return coordinates;
  if (typeof coordinates[0] === "number") {
    return transformMapPosition(coordinates as Position, calibration, origin);
  }
  return coordinates.map((coordinate) =>
    transformCoordinates(coordinate, calibration, origin),
  );
}

export function transformMapFeatures(
  source: FeatureCollection,
  calibration: MapCalibration | undefined,
  origin: MapTransformOrigin | null,
): FeatureCollection {
  const result = JSON.parse(JSON.stringify(source)) as FeatureCollection;
  const transformGeometry = (geometry: Geometry) => {
    if (geometry.type === "GeometryCollection") {
      geometry.geometries.forEach(transformGeometry);
      return;
    }
    const coordinates = geometry as Exclude<Geometry, GeometryCollection>;
    coordinates.coordinates = transformCoordinates(
      coordinates.coordinates,
      calibration,
      origin,
    ) as never;
  };
  result.features.forEach((feature) => {
    if (feature.geometry) transformGeometry(feature.geometry);
  });
  return result;
}
